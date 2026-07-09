"""Behavioral state derived from the audit log — so the Step 0 preflight does
not depend on conversation memory or asking the user (which breaks /loop).

Everything here is READ-ONLY over .audit_log.jsonl. Outputs:

  cooldown      — active if the last resolved trade lost > 0.7R within 24h.
  daily_stop    — active if the trailing 24h realized R or directional loss
                  streak says the day is broken.
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
DAILY_REALIZED_R_THRESHOLD = -2.0
DAILY_DIRECTIONAL_LOSS_THRESHOLD = -0.7
DAILY_DIRECTIONAL_LOSS_N = 2
PASSIVE_TILT_N = 3


def _has_leaks(entry):
    beh = entry.get("behavioral") or {}
    return bool(beh.get("leaks"))


def _passive_tilt(entries):
    """Trailing consecutive resolved passive-fade LOSSES (range-break signal)."""
    passive_resolved = sorted(
        [e for e in entries
         if e.get("setup_family") == "passive-fade" and e.get("outcome")],
        key=lambda e: int(e["outcome"]["resolved_at_ms"]))
    streak = 0
    for e in reversed(passive_resolved):
        if float(e["outcome"]["outcome_r"]) < 0:
            streak += 1
        else:
            break
    return {"active": streak >= PASSIVE_TILT_N, "loss_streak": streak}


def _resolved_rows(entries):
    return sorted(
        [e for e in entries if e.get("outcome")],
        key=lambda e: int(e["outcome"]["resolved_at_ms"]))


def _daily_stop(entries, now_ms):
    """Hard daily-loss gate, based on resolved outcomes only.

    Uses resolve timestamps rather than entry timestamps so late resolves do not
    retroactively block the wrong day. The gate is only as timely as the audit
    log is resolved.
    """
    resolved = _resolved_rows(entries)
    window_start = now_ms - DAY_MS
    recent = [
        e for e in resolved
        if window_start <= int(e["outcome"]["resolved_at_ms"]) <= now_ms
    ]
    realized_r = round(sum(float(e["outcome"]["outcome_r"]) for e in recent), 3)

    active_reasons = []
    untils = []
    if recent and realized_r <= DAILY_REALIZED_R_THRESHOLD:
        last_resolve = max(int(e["outcome"]["resolved_at_ms"]) for e in recent)
        active_reasons.append(f"trailing 24h realized {realized_r}R")
        untils.append(last_resolve + DAY_MS)

    directional_recent = [
        e for e in recent
        if e.get("setup_family") != "passive-fade"
        and e.get("mode") == "ENTRY"
    ]
    streak = []
    for e in reversed(directional_recent):
        r = float(e["outcome"]["outcome_r"])
        if r <= DAILY_DIRECTIONAL_LOSS_THRESHOLD:
            streak.append(e)
        else:
            break
    if len(streak) >= DAILY_DIRECTIONAL_LOSS_N:
        latest = int(streak[0]["outcome"]["resolved_at_ms"])
        active_reasons.append(
            f"{len(streak)} consecutive directional losses <= "
            f"{DAILY_DIRECTIONAL_LOSS_THRESHOLD}R")
        untils.append(latest + DAY_MS)

    until_ms = max(untils) if untils else None
    return {
        "active": bool(untils),
        "until_ms": until_ms,
        "until_utc": (datetime.fromtimestamp(until_ms / 1000, timezone.utc)
                      .strftime("%Y-%m-%d %H:%M UTC") if until_ms else None),
        "realized_r_24h": realized_r,
        "reason": "; ".join(active_reasons) if active_reasons else None,
    }


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
    entry_rows = [e for e in entries
                  if e.get("mode") == "ENTRY"
                  and e.get("setup_family") != "passive-fade"]

    reasons, windows = [], []

    # loss cooldown — most recent resolved trade
    resolved = _resolved_rows(entries)
    if resolved:
        last = max(resolved, key=lambda e: int(e["outcome"]["resolved_at_ms"]))
        r = float(last["outcome"]["outcome_r"])
        end = int(last["outcome"]["resolved_at_ms"]) + LOSS_COOLDOWN_MS
        if r <= LOSS_R_THRESHOLD and now_ms < end:
            reasons.append(f"last trade lost {r}R — 24h cooldown")
            windows.append(end)

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
        "daily_stop": _daily_stop(entries, now_ms),
        "oop_this_week": oop,
        "passive_tilt": _passive_tilt(entries),
    }


def main(argv):
    print(json.dumps(compute_behavioral_state(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
