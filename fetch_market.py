from datetime import datetime, timezone


def iso_utc(ms):
    """Format an epoch-ms timestamp as 'YYYY-MM-DD HH:MM UTC'.

    Always UTC regardless of machine timezone. This function exists to
    structurally prevent the +8h label bug (fromtimestamp without tz).
    """
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
