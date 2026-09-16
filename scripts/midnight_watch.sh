#!/bin/zsh
# Straddle the Asia/Singapore midnight rollover so account_monitor can build a
# near-reset baseline instead of a partial-day one.
#
# account_risk.evaluate() sets baseline_quality="near_reset_observation" only when
# BOTH hold (reset_grace_ms = 60_000):
#   - an observation exists <=60s BEFORE 00:00 SGT  (state.last_observation)
#   - the first observation of the new day is <=60s AFTER 00:00 SGT
# A 15s poll spanning 23:55 -> ~00:02 guarantees both brackets.
#
# caffeinate -i holds off idle sleep for the run only; the Mac is free to sleep
# again once the watch exits. Exit code is non-zero whenever entry is not allowed,
# which is the normal resting state, so it is swallowed deliberately.

set -u
SCALP_DIR="/Users/nyanyk/Claude/research/scalp"
LOG="${SCALP_DIR}/logs/midnight-watch.log"

cd "$SCALP_DIR" || exit 1

echo "=== midnight watch start $(date -u '+%Y-%m-%dT%H:%M:%SZ') / $(date '+%Y-%m-%dT%H:%M:%S %Z') ===" >> "$LOG"

/usr/bin/caffeinate -i /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 account_monitor.py watch \
  --interval-seconds 15 --count 30 --json >> "$LOG" 2>&1
rc=$?

echo "=== midnight watch end   $(date -u '+%Y-%m-%dT%H:%M:%SZ') rc=${rc} ===" >> "$LOG"

# Report the resulting baseline quality so the log answers "did it work?" directly.
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 - <<'PY' >> "$LOG" 2>&1
import json, pathlib
d = pathlib.Path("/Users/nyanyk/Claude/research/scalp/.account_monitor")
try:
    cfg = json.loads((d / "config.json").read_text())
    st = json.loads((d / (cfg["wallet"] + ".json")).read_text())
    day = st.get("day", {})
    print("baseline_quality=%s day=%s baseline_at_ms=%s" % (
        day.get("baseline_quality"), day.get("key"), day.get("baseline_at_ms")))
except Exception as exc:
    print("baseline_quality=UNKNOWN (%s)" % exc)
PY

exit 0
