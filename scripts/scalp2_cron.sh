#!/bin/zsh
# scalp2 US-open scan. Cron runs in LOCAL time; Asia/Singapore has no DST,
# so 20:55 SGT == 12:55 UTC year-round (spec §3).
cd /Users/nyanyk/Claude/research/scalp || exit 1
mkdir -p logs
/usr/bin/env python3 scan2.py --session us >> logs/scan2_cron.log 2>&1
