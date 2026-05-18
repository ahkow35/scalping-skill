from datetime import datetime, timezone


def iso_utc(ms):
    """Format an epoch-ms timestamp as 'YYYY-MM-DD HH:MM UTC'.

    Always UTC regardless of machine timezone. This function exists to
    structurally prevent the +8h label bug (fromtimestamp without tz).
    """
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


from datetime import timedelta

SGT = timezone(timedelta(hours=8))


def build_session(now_ms):
    """Deterministic session/time facts. US cash open 13:30 UTC,
    close 20:00 UTC, econ-data window 12:30 UTC. Asia handoff = the
    2h after US close. All times UTC-anchored."""
    now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    us_open = day + timedelta(hours=13, minutes=30)
    us_close = day + timedelta(hours=20)
    econ = day + timedelta(hours=12, minutes=30)
    h_open = (us_open - now).total_seconds() / 3600
    h_close = (us_close - now).total_seconds() / 3600
    us_live = us_open <= now < us_close
    return {
        "utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "sgt": now.astimezone(SGT).strftime("%Y-%m-%d %H:%M SGT"),
        "hours_to_us_open": round(h_open, 2),
        "hours_to_us_close": round(h_close, 2),
        "hours_to_econ_window": round((econ - now).total_seconds() / 3600, 2),
        "us_session_live": us_live,
        "asia_handoff_soon": us_close <= now < us_close + timedelta(hours=2),
    }


from collections import defaultdict


def band_aggregate(levels, bucket=0.05):
    """Sum USDC notional (px*sz) into nearest price buckets of width `bucket`."""
    agg = defaultdict(float)
    inv = 1.0 / bucket
    for lv in levels:
        p = float(lv["px"])
        b = round(round(p * inv) / inv, 2)
        agg[b] += p * float(lv["sz"])
    return dict(agg)
