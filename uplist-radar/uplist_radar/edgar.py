"""SEC EDGAR client: polite, cached, retrying access to public EDGAR endpoints.

Endpoints used (all public, no key required):
  - https://efts.sec.gov/LATEST/search-index   (full-text search)
  - https://data.sec.gov/submissions/CIK##########.json
  - https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/index.json
  - https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{doc}
  - https://www.sec.gov/files/company_tickers.json

Politeness: minimum interval between requests, exponential backoff on
429/5xx, identifying User-Agent with contact email (required by SEC).
Documents are cached on disk under data/filings/.
"""
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger("uplist_radar.edgar")

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class EdgarError(Exception):
    pass


class EdgarClient:
    def __init__(self, db=None, min_interval=None, max_retries=None, session=None):
        self.db = db
        self.min_interval = min_interval if min_interval is not None else config.SEC_MIN_INTERVAL
        self.max_retries = max_retries if max_retries is not None else config.SEC_MAX_RETRIES
        self._last_req = 0.0
        self.session = session or requests.Session()
        self._ua = None

    @property
    def user_agent(self):
        if self._ua is None:
            self._ua = config.sec_user_agent(self.db)
        return self._ua

    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def get(self, url, timeout=30, accept=None):
        """GET with throttle + exponential backoff. Returns response or raises."""
        headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
        if accept:
            headers["Accept"] = accept
        last_exc = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    wait = min(60, 2 ** attempt)
                    log.warning("SEC %s for %s; backing off %ss (attempt %d)",
                                resp.status_code, url, wait, attempt + 1)
                    time.sleep(wait)
                    continue
                if resp.status_code == 404:
                    raise EdgarError(f"404 not found: {url}")
                resp.raise_for_status()
                return resp
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                wait = min(60, 2 ** attempt)
                log.warning("SEC connection error for %s; backing off %ss (attempt %d): %s",
                            url, wait, attempt + 1, e)
                time.sleep(wait)
        raise EdgarError(f"GET failed after {self.max_retries} attempts: {url} ({last_exc})")

    # ---------- full-text search ----------
    def fts_search(self, phrase, forms, start_date, end_date, size=100):
        """Yield raw FTS hits for a phrase across forms in a date window."""
        q = phrase if " " not in phrase.strip('"') else phrase
        from_ = 0
        total = None
        while True:
            params = {
                "q": f'"{phrase}"',
                "forms": ",".join(forms),
                "startdt": start_date,
                "enddt": end_date,
                "size": size,
                "from": from_,
            }
            url = FTS_URL + "?" + "&".join(
                f"{k}={quote(str(v), safe=',')}" for k, v in params.items())
            data = self.get(url, accept="application/json").json()
            hits = data.get("hits", {}).get("hits", [])
            if total is None:
                total = data.get("hits", {}).get("total", {}).get("value", 0)
            if not hits:
                break
            for h in hits:
                yield h.get("_source", {}), h.get("_id", "")
            from_ += len(hits)
            if from_ >= (total or 0):
                break

    # ---------- company submissions ----------
    def submissions(self, cik10):
        url = SUBMISSIONS_URL.format(cik=cik10)
        return self.get(url, accept="application/json").json()

    # ---------- filing index & documents ----------
    @staticmethod
    def adsh(accession):
        return accession.replace("-", "")

    def filing_index(self, cik10, accession):
        url = ARCHIVE_URL.format(cik=cik10, adsh=self.adsh(accession)) + "/index.json"
        return self.get(url, accept="application/json").json()

    def primary_doc_name(self, cik10, accession, form_hint=""):
        """Pick the primary document from the filing index.

        Prefers a document whose name contains the form slug (e.g. form8-a12b.htm),
        skips exhibits (ex*.htm) and R-file financial-report pages, then falls
        back to the first remaining .htm, then the complete .txt.
        """
        idx = self.filing_index(cik10, accession)
        items = idx.get("directory", {}).get("item", [])
        names = [i["name"] for i in items
                 if not i["name"].endswith(("-index.html", "-index-headers.html"))]
        htm = [n for n in names
               if n.lower().endswith((".htm", ".html"))
               and not re.match(r"(?i)^(ex|exhibit)[\-_0-9]", n)
               and not re.match(r"(?i)^r\d+\.htm", n)]
        if form_hint and htm:
            slug = re.sub(r"[^a-z0-9]", "", form_hint.lower())
            for n in htm:
                if slug and slug in re.sub(r"[^a-z0-9]", "", n.lower()):
                    return n
        if htm:
            return htm[0]
        txt = [n for n in names if n.lower().endswith(".txt")]
        return txt[0] if txt else (names[0] if names else None)

    def cache_path(self, cik10, accession, doc_name):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", doc_name)
        return config.FILINGS_DIR / cik10 / self.adsh(accession) / safe

    def fetch_document(self, cik10, accession, doc_name=None):
        """Download (or reuse cached) filing document. Returns (path, bytes)."""
        if doc_name is None:
            doc_name = self.primary_doc_name(cik10, accession)
            if not doc_name:
                raise EdgarError(f"No documents in filing {accession}")
        path = self.cache_path(cik10, accession, doc_name)
        if path.exists() and path.stat().st_size > 0:
            return path, path.read_bytes()
        url = ARCHIVE_URL.format(cik=cik10, adsh=self.adsh(accession)) + "/" + quote(doc_name)
        resp = self.get(url, timeout=60)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return path, resp.content

    def document_url(self, cik10, accession, doc_name):
        return ARCHIVE_URL.format(cik=cik10, adsh=self.adsh(accession)) + "/" + quote(doc_name)

    def filing_url(self, cik10, accession):
        return ARCHIVE_URL.format(cik=cik10, adsh=self.adsh(accession)) + "/"

    # ---------- text extraction ----------
    @staticmethod
    def html_to_text(content: bytes) -> str:
        if content[:5].lower() == b"<?xml" or b"<SEC-DOCUMENT>" in content[:2000].upper():
            # SGML-ish .txt filing: strip tags crudely
            text = re.sub(rb"<[^>]+>", b" ", content)
            return re.sub(rb"\s+", b" ", text).decode("utf-8", "ignore")
        soup = BeautifulSoup(content, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        # Remove XBRL hidden sections which add noise
        for tag in soup.find_all(["ix:header", "ix:hidden"]):
            tag.decompose()
        text = soup.get_text(separator=" ")
        text = re.sub(r"[ \t\xa0]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    # ---------- ticker map ----------
    _ticker_map = None

    def ticker_map(self):
        if EdgarClient._ticker_map is None:
            cache = config.DATA_DIR / "company_tickers.json"
            if cache.exists():
                data = json.loads(cache.read_text())
            else:
                data = self.get(TICKERS_URL, accept="application/json").json()
                cache.write_text(json.dumps(data))
            m = {}
            for _k, v in data.items():
                m[v["ticker"].upper()] = str(v["cik_str"]).zfill(10)
            EdgarClient._ticker_map = m
        return EdgarClient._ticker_map

    def cik_for_ticker(self, ticker):
        return self.ticker_map().get(ticker.upper().strip())

    # ---------- company facts (XBRL) ----------
    def companyfacts(self, cik10):
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
        return self.get(url, accept="application/json", timeout=45).json()
