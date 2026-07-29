#!/bin/zsh
# scalp2 US-open scan. Cron runs in LOCAL time; Asia/Singapore has no DST,
# so 20:55 SGT == 12:55 UTC year-round (spec §3).
cd /Users/nyanyk/Claude/research/scalp || exit 1
mkdir -p logs
# Pinned interpreter: cron's minimal PATH resolves the system Python 3.9,
# which reproducibly fails on Hyperliquid responses (http.client
# IncompleteRead). The suite + live acceptance ran on this 3.14 install.
PYBIN=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
[ -x "$PYBIN" ] || { echo "$(date -u '+%Y-%m-%d %H:%M') PYBIN missing: $PYBIN" >> logs/scan2_cron.log; exit 1; }
"$PYBIN" scan2.py --session us >> logs/scan2_cron.log 2>&1
