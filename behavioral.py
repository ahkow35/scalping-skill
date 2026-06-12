"""Behavioral state derived from the audit log — so the Step 0 preflight does
not depend on conversation memory or asking the user (which breaks /loop).

Everything here is READ-ONLY over .audit_log.jsonl. Three outputs:

  cooldown      — active if the last resolved trade lost > 0.7R within 24h,
                  OR the last 2 ENTRY decisions both showed FOMO leaks (3-day).
  fomo_streak   — trailing count of consecutive ENTRY decisions with leaks.
  oop_this_week — count of OOP-1-tagged entries since the Saturday week start
                  (derives going forward — older entries predate plan_status).

CLI:
  python3 behavioral.py            # JSON state, now = wall clock
"""

import json
import sys
import time
from datetime import datetime, timezone, timedelta

import audit_log

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
LOSS_R_THRESHOLD = -0.7
LOSS_COOLDOWN_MS = DAY_MS
FOMO_COOLDOWN_MS = 3 * DAY_MS


def _has_leaks(entry):
    beh = entry.get("behavioral") or {}
    return bool(beh.get("leaks"))


def _week_start_saturday_ms(now_ms):
    """Most recent Saturday 00:00 UTC at or before now (planning week start)."""
    now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # weekday(): Mon=0 .. Sat=5, Sun=6
    days_since_sat = (now.weekday() - 5) % 7
    start = midnight - timedelta(days=days_since_sat)
    return int(start.timestamp() * 1000)


def compute_behavioral_state(now_ms=None, path=None):
    now_ms = now_ms or int(time.time() * 1000)
    entries = sorted(audit_log._load_entries(path), key=lambda e: int(e["ts_ms"]))
    entry_rows = [e for e in entries if e.get("mode") == "ENTRY"]

    reasons, windows = [], []

    # loss cooldown — most recent resolved trade
    resolved = [e for e in entries if e.get("outcome")]
    if resolved:
        last = max(resolved, key=lambda e: int(e["outcome"]["resolved_at_ms"]))
        r = float(last["outcome"]["outcome_r"])
        end = int(last["outcome"]["resolved_at_ms"]) + LOSS_COOLDOWN_MS
        if r <= LOSS_R_THRESHOLD and now_ms < end:
            reasons.append(f"last trade lost {r}R — 24h cooldown")
            windows.append(end)

    # fomo cooldown — last 2 ENTRY decisions both leak
    if len(entry_rows) >= 2 and _has_leaks(entry_rows[-1]) \
            and _has_leaks(entry_rows[-2]):
        end = int(entry_rows[-1]["ts_ms"]) + FOMO_COOLDOWN_MS
        if now_ms < end:
            reasons.append("last 2 entries were FOMO-form — 3-day cooldown")
            windows.append(end)

    # fomo streak — trailing consecutive leaking ENTRY decisions
    streak = 0
    for e in reversed(entry_rows):
        if _has_leaks(e):
            streak += 1
        else:
            break

    # oop count this week
    week_start = _week_start_saturday_ms(now_ms)
    oop = sum(1 for e in entries
              if e.get("plan_status") == "OOP-1"
              and int(e["ts_ms"]) >= week_start)

    until_ms = max(windows) if windows else None
    return {
        "cooldown": {
            "active": bool(windows),
            "until_ms": until_ms,
            "until_utc": (datetime.fromtimestamp(until_ms / 1000, timezone.utc)
                          .strftime("%Y-%m-%d %H:%M UTC") if until_ms else None),
            "reasons": reasons,
        },
        "fomo_streak": streak,
        "oop_this_week": oop,
    }


def main(argv):
    print(json.dumps(compute_behavioral_state(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
