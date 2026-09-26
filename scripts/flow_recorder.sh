#!/bin/zsh
# WebSocket flow recorder — research only, does NOT feed the live /scalp
# flow gate (see recorder.py's module docstring and SKILL.md). Meant to run
# continuously under launchd (com.nyanyk.scalp-flow-recorder.plist), which
# captures stdout/stderr to logs/.
cd /Users/nyanyk/Claude/research/scalp || exit 1
mkdir -p logs
# Pinned interpreter: launchd's minimal PATH resolves the system Python 3.9,
# which reproducibly fails on Hyperliquid responses (http.client
# IncompleteRead) — see scripts/scalp2_cron.sh. The suite ran on this 3.14
# install.
PYBIN=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
[ -x "$PYBIN" ] || { echo "$(date -u '+%Y-%m-%d %H:%M') PYBIN missing: $PYBIN" >> logs/flow_recorder.log; exit 1; }
# exec (not a plain call): this shell process is REPLACED by caffeinate, so
# launchd's SIGTERM — sent to this script's own pid — reaches caffeinate
# directly, which is expected to forward it to recorder.py, giving the
# recorder a clean shutdown (flush + close) instead of being orphaned or
# killed hard. Not verified against a real launchd load in this build
# (the plist is intentionally not installed) — confirm at install time.
# caffeinate -s: hold a system-sleep assertion for as long as the recorder
# runs (decision 2 in tasks/todo.md — keep the Mac awake on mains power so
# nightly sleep holes mostly disappear).
exec /usr/bin/caffeinate -s "$PYBIN" recorder.py --coins HYPE
