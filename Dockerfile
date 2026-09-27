# Railway service for the read-only Hyperliquid account watcher
# (railway_watch.py). Never places, cancels or closes an order — see
# ACCOUNT-MONITOR.md and the module docstring.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# tzdata: zoneinfo needs the IANA database, and slim images don't ship it —
# every check would otherwise fail on the configured timezone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir requests

# Only the files the watcher actually imports — not the rest of the
# research repo (recorder, backtests, card scans, etc. are out of scope
# for this service).
COPY account_api.py account_observation.py account_risk.py account_monitor.py railway_watch.py ./

CMD ["python3", "railway_watch.py"]
