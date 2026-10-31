FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/uplist-radar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m uplist && mkdir -p /var/lib/uplist-radar && chown uplist:uplist /var/lib/uplist-radar
ENV UPLIST_DATA_DIR=/var/lib/uplist-radar TZ=America/Los_Angeles
USER uplist
EXPOSE 8765
# Set UPLIST_SEC_CONTACT, UPLIST_MASTER_KEY, UPLIST_FLASK_SECRET at deploy time.
CMD ["gunicorn","-w","1","--threads","4","-b","0.0.0.0:8765","uplist_radar.app:create_app()"]
