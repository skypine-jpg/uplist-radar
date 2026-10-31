"""Market-data providers.

Interface: get_quote(ticker) -> dict with keys:
  price, currency, volume, market_cap, shares_outstanding, quote_time,
  is_delayed, provider, as_of, error

Providers:
  - Stooq (free, no key): best-effort; OTC coverage is spotty. Quotes delayed.
  - SEC Company Facts (XBRL): shares outstanding / balance-sheet facts. Free.
  - Licensed providers (OTC Markets, Polygon, Nasdaq Data Link): NOT configured.
    Configure via Admin -> Providers. Until then fields show "Unknown".

Never fabricate prices or market caps: missing data is returned as None and
displayed as "Unknown" with the provider status.
"""
import csv
import io
import logging
import re
import time
from datetime import datetime, timezone

import requests

from . import config

log = logging.getLogger("uplist_radar.marketdata")

STOOQ_URL = "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"


def _stooq_symbol(ticker):
    return f"{ticker.lower()}.us"


def get_stooq_quote(ticker, timeout=20):
    """Best-effort delayed quote from Stooq. Returns dict (see module docstring)."""
    out = {"provider": "stooq", "ticker": ticker.upper(), "as_of": _now(),
           "is_delayed": True, "error": None}
    try:
        resp = requests.get(STOOQ_URL.format(sym=_stooq_symbol(ticker)),
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        if not rows:
            out["error"] = "no data"
            return out
        r = rows[0]
        if r.get("Close") in (None, "", "N/D"):
            out["error"] = "no quote for symbol"
            return out
        out.update({
            "price": _f(r.get("Close")),
            "volume": _i(r.get("Volume")),
            "quote_time": f"{r.get('Date')} {r.get('Time')}",
            "currency": "USD",
        })
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _i(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- SEC Company Facts (XBRL) ----------
FACT_TAGS = {
    "shares_outstanding": [("us-gaap", "EntityCommonStockSharesOutstanding"),
                           ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic")],
    "cash": [("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
             ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")],
    "total_assets": [("us-gaap", "Assets")],
    "total_liab": [("us-gaap", "Liabilities")],
    "equity": [("us-gaap", "StockholdersEquity"),
               ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")],
    "revenue": [("us-gaap", "Revenues"), ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax")],
    "net_income": [("us-gaap", "NetIncomeLoss")],
}


def extract_companyfacts(facts_json):
    """Pull latest annual/quarterly values for key tags. Returns {tag: {value, end, form}}."""
    out = {}
    try:
        gaap = facts_json.get("facts", {})
        for tag, candidates in FACT_TAGS.items():
            for ns, name in candidates:
                units = gaap.get(ns, {}).get(name, {}).get("units", {})
                vals = units.get("USD", []) or units.get("shares", [])
                # prefer 10-K then 10-Q, latest end date
                best = None
                for v in vals:
                    if v.get("form") not in ("10-K", "10-Q", "20-F"):
                        continue
                    if best is None or v.get("end", "") > best.get("end", "") or \
                            (v.get("end") == best.get("end") and v.get("form") == "10-K"):
                        best = v
                if best:
                    out[tag] = {"value": best.get("val"), "end": best.get("end"),
                                "form": best.get("form"), "filed": best.get("filed")}
                    break
    except Exception as e:
        log.warning("companyfacts extraction failed: %s", e)
    return out


def summarize_financials(facts):
    """Human-readable financial summary with explicit N/A for missing values."""
    def fmt(tag, money=True):
        d = facts.get(tag)
        if not d or d.get("value") is None:
            return "Unknown"
        v = d["value"]
        if money:
            return f"${v:,.0f} (as of {d.get('end')}, {d.get('form')})"
        return f"{v:,.0f} (as of {d.get('end')}, {d.get('form')})"
    return {
        "shares_outstanding": fmt("shares_outstanding", money=False),
        "cash": fmt("cash"),
        "total_assets": fmt("total_assets"),
        "total_liabilities": fmt("total_liab"),
        "stockholders_equity": fmt("equity"),
        "revenue_ttm": fmt("revenue"),
        "net_income": fmt("net_income"),
    }


def market_snapshot_for_company(db, edgar, company):
    """Build/persist a market snapshot: quote (best-effort) + XBRL facts."""
    from .db import utcnow
    ticker = (company["ticker"] or "").strip()
    snap = {"provider": "stooq+companyfacts", "as_of": utcnow(), "is_delayed": True}
    if ticker:
        q = get_stooq_quote(ticker)
        snap.update({k: q.get(k) for k in
                     ("price", "currency", "volume", "quote_time", "error")})
        if q.get("price") and not q.get("error"):
            db.execute("UPDATE providers SET status='degraded', last_used_at=? WHERE key='stooq'",
                       (utcnow(),))
    else:
        snap["error"] = "no ticker"
    # XBRL facts
    try:
        facts = edgar.companyfacts(company["cik"])
        extracted = extract_companyfacts(facts)
        snap["shares_outstanding"] = (extracted.get("shares_outstanding") or {}).get("value")
        snap["facts"] = extracted
        db.execute("UPDATE providers SET status='ok', last_used_at=? WHERE key='companyfacts'",
                   (utcnow(),))
    except Exception as e:
        snap["facts_error"] = str(e)[:200]
        log.warning("companyfacts failed for %s: %s", company["cik"], e)
    # market cap only when both price and shares known
    if snap.get("price") and snap.get("shares_outstanding"):
        snap["market_cap"] = snap["price"] * snap["shares_outstanding"]
    db.execute(
        """INSERT INTO market_snapshots(company_id,as_of,provider,price,currency,volume,
           market_cap,shares_outstanding,quote_time,is_delayed,raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (company["id"], snap["as_of"], snap["provider"], snap.get("price"),
         snap.get("currency"), snap.get("volume"), snap.get("market_cap"),
         snap.get("shares_outstanding"), snap.get("quote_time"),
         1 if snap.get("is_delayed") else 0,
         __import__("json").dumps({k: v for k, v in snap.items() if k != "facts"})))
    return snap
