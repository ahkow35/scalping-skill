# Recorder tape feeds the /scalp flow gate (plan, 2026-10-04)

Status: MERGED and LIVE (PR #24, squash 31776fa, 2026-10-05 00:21 SGT). Nyan approved the plan and decided: add PUMP; fail closed; draft PR. BTC added to the recorder at Nyan's request. Three Codex cross-review rounds (7 findings, all fixed; the last fix, 0985ff0, was not re-reviewed). RECORDER_COINS needed a separate `railway config apply --yes`, run by Nyan (Claude is blocked from applying). Verified live 00:41 SGT: HYPE, ZEC, PUMP, BTC all read from the recorder at 100% 5m coverage. Open: check the recorder volume's free space after BTC's first full day.

## Objective

Let `/scalp`'s flow gate (Step 1b: are buyers or sellers the aggressors?) read the complete trade tape the Railway recorder already captures, instead of the 10-trades-per-call REST sample that can never prove coverage. Today that gate returns WAIT on every entry read for every coin.

## What this changes, plainly

- It does not add a signal or loosen a threshold. The gate stays cut-only: it can lower conviction or force WAIT, never raise it.
- It does remove a standing block. With reliable flow, action verdicts with real size become possible again. Entry alpha is still unvalidated; SKILL.md says so and keeps saying so.
- Coins the recorder does not capture keep getting WAIT, exactly as today.

## Facts found (2026-10-04)

- The live tape is on the Railway recorder's disk. The Mac's `.flow_data` stopped on 2026-07-12. The storage bucket only holds finished days.
- The recorder has no public address by design (RECORDER-RAILWAY.md). The watcher is the public, token-protected face `/scalp` already reads.
- The recorder captures HYPE and ZEC only. PUMP is not recorded.
- `flow.py` already accepts a reliable non-REST source: `_reliable()` needs `reliable`, `capture_complete`, coverage of 50% or more and a last trade under 60 seconds old. No threshold changes needed.

## Decisions needed (Nyan)

1. Record PUMP as well (Railway setting change, roughly 20 MB a day more), or HYPE and ZEC only?
2. Fail closed: recorder unreachable, stale or with a gap in the window means WAIT, with no fallback to the REST sample. Recommended.
3. Build to a draft PR. Merging is Nyan's call and redeploys both Railway services (never within 10 minutes of midnight Singapore time).

## Build (one PR, additive)

- [x] `recorder.py` / `railway_record.py`: `GET /flow?coin=X` on the recorder's private port. Returns per-minute rows for the trailing 4 hours from today's (and, near midnight, yesterday's) trade file: `t_ms, buy_usdc, sell_usdc, count`, plus `last_trade_ms` and the gap records inside the window. Deduped by trade id. No gate logic on Railway.
- [x] `railway_watch.py`: a token-protected `GET /flow?coin=X` that fetches the recorder's `/flow` over the private network at request time and passes it through. Same bearer token as `/report`. `/report` and `entry_allowed` are untouched. (Chosen over attaching flow to `/report`: the once-a-minute poll would eat most of the 60-second freshness budget.)
- [x] `fetch_market.py`: a second builder beside `bucket_taker_delta` that turns those minute rows into the 5m/15m/1h/4h windows with `source: "ws_recorder"`. `coverage_pct` = recorded trade count ÷ the exchange's own per-candle trade count over the window's closed 1-minute bars. `capture_complete` only when every bar had a count and no gap falls in the window. `reliable` only when coverage is 50% or more and the last trade is under 60 seconds old. Unknown stays `null`, never 0. No tape, stale tape, or an unrecorded coin falls back to today's REST output, which stays `reliable: false`.
- [x] `flow.py`: no change expected; confirm with a test.
- [x] Tests, no network: coverage maths; a gap makes the window not complete; a stale last trade makes it not reliable; an unrecorded coin stays unreliable; a regression test that the REST builder still returns `reliable: false`; the watcher's `/flow` refuses a missing or wrong token and never alters `/report`.
- [x] Docs in the same PR: SKILL.md "Note on taker_delta cache", scalp-core.md Step 1/1b, RECORDER-RAILWAY.md, ACCOUNT-MONITOR.md, CHANGELOG, and a short ADR (the recorder becomes a live input).
- [x] If decision 1 is yes: `RECORDER_COINS: "HYPE,ZEC,PUMP,BTC"` (BTC added at Nyan's request) in `.railway/railway.ts`.

## Deploy (gated: needs Nyan's OK at the time)

- Nyan merges; Railway redeploys both services. Not within 10 minutes of midnight Singapore time.
- Acceptance: `/scalp HYPE` shows a FLOW line with a real buyer/seller bias and coverage above 50%; `/scalp` on an unrecorded coin still says flow unavailable; stopping the recorder makes HYPE fall back to WAIT.

## Out of scope

- Any new signal, threshold change or indicator. Order placement. Running the recorder on the Mac (it sleeps; that is why it moved).

## Risks

- **Live entries become possible again** on unvalidated entry logic. The other gates (account, daily stop, behavioural, macro, net reward-to-risk) are unchanged.
- The 58/42 buyer/seller thresholds in `flow.py` are untested candidates; they have never run against a complete tape.
- A thin coin in a quiet minute will fail the 60-second freshness rule. That is the gate working; do not pad it.

---

# Previous plan in this file: always-on watcher on Railway (plan, 2026-09-27)

Status: DRAFT v3. Nyan's decisions recorded 2026-09-27; all six settled. Nothing is built or deployed.

## Objective

Move the account monitor's watching off the Mac onto an always-on Railway service that checks the account every 15 to 30 seconds and sends a Telegram alert when something needs attention: a position without a full stop, a position over the size cap, an add to a losing position, the daily loss limit tripped, open stop risk bigger than the day's remaining budget, or the monitor itself unable to see the account. `/scalp` reads its entry answer from this service from day one, and the flow recorder runs alongside it.

Why: a replay of the owner's real fills (2026-09-27) showed nearly all losses came from three trades that were oversized, had no stop and were averaged down, all in trades `/scalp` never saw. The monitor already detects most of this, but only when someone runs it. Nothing here can place, cancel or close an order; it watches and alerts.

## Decisions (Nyan, 2026-09-27)

1. **Alert channel:** a private Telegram bot that messages only Nyan.
2. **Watcher liveness:** a free dead-man check (for example healthchecks.io) that alerts if the watcher stops checking in for 5 minutes, plus a short daily "alive" message.
3. **Repeats:** alert when a problem appears, repeat every 30 minutes while it lasts, "all clear" when it ends.
4. **Recorder:** moves to Railway together with the watcher, as its own process in the same project. Waits on PR #12 (round-3 review running) merging first.
5. **`/scalp` entry check reads Railway from day one.** The service exposes the latest check report over a small read-only HTTPS address protected by a read token kept in the Mac's Keychain. `/scalp` accepts it only if it is fresh (under 60 seconds old); unreachable, stale or failed means no new entry (fail closed, same as today's missing-data rule). MANAGE mode is unaffected. The Mac midnight job and the local check are retired once this is live; the local check stays in the code for manual use.
6. **Recorded day files go to Supabase Storage** (Nyan, 2026-09-27): a private bucket, uploaded by the recorder after each UTC-day rotation with a key limited to that bucket; the Railway disk keeps a few days as a buffer, and a file is removed locally only after its upload is confirmed. Measured July sample: about 13 KB per minute compressed for HYPE, so roughly 20 MB a day, 0.6 GB a month, well inside Pro's 100 GB.

## Steps you do (no one else can)

- ~~Create the Railway account~~ Done 2026-09-27: Hobby plan ($5/month incl. $5 usage).
- ~~Let Railway's GitHub app see the repo~~ Done 2026-09-27.
- Create the Telegram bot and paste its token and your chat id into Railway's settings page yourself. Never into chat.
- Create the dead-man check (healthchecks.io) and paste its ping address into Railway the same way.
- Before PR C: create a private Supabase Storage bucket and paste a key limited to it into Railway.

## Build (three PRs in order; research repo, additive)

**PR A — watcher** (items 1–12 below). **PR B — `/scalp` reads the watcher** (item 13). **PR C — recorder on Railway + Supabase upload** (item 14). Sequential because they share the Railway service files. Railway layout: one project, two services from the same repo (watcher, recorder), each with its own start command and its own small disk.


1. **One small watcher program, not a wrapper around the existing watch command.** It calls the monitor's check directly in a loop, works out the set of current problems from the check's structured data, compares it with the previous set, and decides what to send. No parsing of printed text, and every rule can be tested with a fake check. Reason: the monitor's single status word shows only the most serious problem at a time (a tripped loss limit hides a new unprotected position), and the two new warnings are text only.
2. **Problems come from structured fields,** not the status word or reason text: each position's stop coverage, the loss latch, open stop risk against the remaining budget, and machine-readable flags for oversized positions and underwater adds. The in-flight warnings PR should expose those two as flags alongside its text; if it lands without them, a small follow-up adds them.
3. **"All clear" only from a good reading.** A failed check returns a report with no positions in it, which must never be read as "problem gone". A problem ends only when a successful check shows it gone.
4. **Monitor failure is its own alert,** counted as consecutive failed checks over 5 minutes, so a single blip (the exchange's occasional read race) doesn't flap, and a real outage does alert.
5. **Midnight.** Check every 15 seconds from 23:58 to 00:02 Singapore time so the watcher's own starting balance for the day is trustworthy, and never deploy within 10 minutes of midnight (a restart across it leaves a partial day).
6. **Alerts never slow the checks.** Telegram gets a short timeout; a failed send is logged and retried at the next repeat, and never stops the loop. Messages say what is wrong in plain words and the coin, and never include secrets.
7. **Dead-man ping** on every loop pass that produced a report, good or failed, so it proves the program is alive; data problems are covered by item 4.
8. **Config applied on every boot,** using the monitor's existing configure step from the service's settings (public wallet, time zone, daily loss limit, size cap), with state on a small Railway disk. The configure step already refuses a time-zone change while state exists and keeps the day's loss latch.
9. **Container details.** Include time-zone data in the image (slim images often lack it, which would make every check fail); run one copy only (a second copy is refused by the monitor's lock, which shows as failed checks rather than double alerts).
10. **Tests with no network:** each problem appearing, repeating and clearing; a failed check never producing an all-clear; the 5-minute failure rule including a single blip; the midnight fast-poll window; a Telegram failure not stopping the loop; config applied on boot.
11. **Latest-report address.** The watcher serves its most recent report (with its timestamp) at one read-only web address on Railway's public domain, protected by a read token from the service settings; no token or a wrong token gets nothing. Standard library only, no new web framework.
12. **Docs.** ACCOUNT-MONITOR.md gains a Railway section, including that **merging to main redeploys the watcher** once Railway follows the main branch, and not to merge near midnight Singapore time.
13. **PR B — `/scalp` entry preflight reads the watcher.** The Mac fetches the latest report with the read token (kept in the Mac's Keychain, added by Nyan himself). Accept only a report under 60 seconds old; unreachable, stale, malformed or failed means no new entry. MANAGE mode unaffected. The local check stays available by hand. After 48 hours green, Nyan removes the Mac midnight job (system state, his OK).
14. **PR C — recorder on Railway.** Runs the merged recorder as the second service with its own disk. After each UTC-day rotation, uploads the finished compressed files to a private Supabase Storage bucket; removes a local file only after the upload is confirmed and a few days have passed; a failed upload retries and never stops recording. Its status (connected, gaps, last upload) joins the watcher's alerts: recorder silent for 5 minutes or an upload failing for a day raises a Telegram alert.

## Deploy (gated: needs Nyan's OK at the time)

- Nyan merges the PR (merge is always on his ask), then approves the first deploy, not within 10 minutes of midnight Singapore time.
- Acceptance: a test alert arrives on the phone; temporarily setting a tiny size cap produces a real "oversized" alert and then an all-clear; pausing the service makes the dead-man check alert within 5 minutes; 48 hours of running with no crash, including one midnight with a trustworthy starting balance recorded.

## Done means

- Tests green, draft PR reviewed.
- The acceptance checks above all observed, with the alert messages quoted in the report.

## Out of scope

- Anything that places, cancels or closes orders.
- Making the new warnings block `/scalp` entries (separate decision).
- Making `/scalp` block on anything beyond today's rules (it only changes WHERE the answer comes from).
- The edge research (separate plan).

## Risks

- **False comfort.** Alerts depend on Telegram and Railway both working; the dead-man check covers Railway, not a phone on silent.
- **Alert fatigue.** The 30-minute repeat and all-clear rule exist to prevent it; revisit after a week.
- **Merge becomes deploy,** and a deploy near midnight costs that day's trustworthy starting balance.
- **Cost.** Usage-based billing; the watcher is tiny, the recorder later adds disk and network.

---

# Previous plan in this file: flow recorder revival (2026-09-27)

Built as draft PR #12 (research-only), two Codex cross-review rounds applied. Its text follows unchanged.

# Flow recorder revival (plan, 2026-09-27)

Status: DRAFT v2 (after the fresh-context critique), awaiting Nyan's two decisions below. The previous plan in this file (account protection and scalp integrity) shipped as PRs #6–#10.

## Objective

Bring the WebSocket flow recorder built in July on the local-only branch `fix/flow-honesty` onto current main. Then run it unattended long enough to collect a complete, honestly-gapped HYPE tape: trades (with buyer and seller wallets), order-book snapshots and best bid/ask. This is step 1 of the sequence agreed in July: record the tape, validate one historical day, run three pre-registered out-of-sample flow tests, and keep flow out of conviction until one passes.

**What this does NOT change:** `/scalp` verdicts. The live flow gate keeps treating flow as unconfirmed, so `/scalp` keeps ending at WAIT on flow exactly as it does today. Wiring the recorder into the gate is a separate, later decision (PR B below).

## Decisions for Nyan (one round)

1. **Scope.** Recommended: research-only now (PR A). Alternative: also wire the recorder into the live flow gate now. That lets `/scalp` fire again, but it contradicts the July rule that flow stays out of conviction until a test passes.
2. **Running it unattended on this Mac.** Recommended: install a LaunchAgent (a macOS background job that restarts the recorder if it dies), and keep the Mac awake while it's on mains power so the nightly sleep holes mostly disappear. Both are system-state changes, so approving this approves them. Revisit after one week using the recorded gap totals. Alternative: run it by hand only for the acceptance test, and install nothing.

Settled by the critique, no decision needed: keep all three streams. Best bid/ask updates about every 0.14 s against about every 5 s for order-book snapshots, and the absorption/refill test needs that resolution. Never delete tape automatically: the flow tests need months of data, and the S3 validation day must still exist when AWS access arrives.

## What survives from the old branch

- Keep: the recorder module and its tests, the July plan document (as history), and the ignore rule for the recorder's data folder.
- Keep one small piece of the "sample_pct" commit: the market fetcher's candle output must carry the exchange's per-candle trade count. Current main drops it, which silently breaks the recorder's self-check (the unit test hides this because its fake candles include the count). Add a test against the real candle shape.
- Drop the rest of that commit (renaming the flow "coverage" field inside the fetcher and the gate). PR #7 has since solved the same honesty problem differently.
- Drop the July skill-instruction edits and changelog entry, and write fresh ones.

## Build steps (PR A, additive, research-only)

1. Start a fresh branch from main. Bring over the recorder module, its tests and the plan document, add the data-folder ignore rule, and add the trade-count field to the candle output.
2. **Detect dead connections.** Today a half-open socket after sleep or a network change blocks forever: the status still says connected, no gap record is written and nothing reconnects. Add a receive watchdog so that no message on any stream for 30 seconds forces a reconnect, which writes gap records as normal. Also stop silently swallowing errors in the connect loop and log each one.
3. **Compress at rotation, inside the recorder.** When a stream rolls to a new UTC day, the recorder closes and compresses the finished file itself, so nothing races an open file. The self-check, and any future reader, must read both compressed and plain files.
4. **Disk floor, alert only.** If free disk falls below a floor (default 10 GB), say so in the status file and the log. Never delete. For scale: about 20 MB a day compressed for all three streams (estimated, to be measured in week one), which is roughly 7 GB a year.
5. **Status file.** Add time since the last message, reconnects in the last 24 hours and total gap time. Clear the in-memory de-duplication set at each UTC day roll. The exchange replays about 30 seconds of trades on subscribe, so readers must de-duplicate by trade ID anyway, and the analysis notes will say so.
6. **LaunchAgent (only if decision 2 is approved).** A keep-alive job (restart on exit, with a throttle so a crash loop can't spin) and a wrapper script that hands the process over directly, so the shutdown signal reaches the recorder and it closes its files cleanly. Keep the Mac awake on mains power while the recorder runs. Logs go to the repo's logs folder.
7. Update the skill notes that describe the recorder as "not implemented": a recorder exists for research, and it does not feed the live gate.

## Done means

- Full test suite green, including recorder tests with no live network.
- A fresh 10-minute live run on this Mac. Its self-check shows captured trades at or near 100% of the exchange's own trade count, as July did (945 of 945).
- A forced network drop mid-run produces gap records in every open file, and the recorder reconnects on its own within the watchdog window.
- A sleep-and-wake test: put the Mac to sleep for a few minutes. On wake the watchdog fires, gap records appear and recording resumes, with no hang.
- The day-rotation compression is exercised in a test with a faked clock crossing UTC midnight.
- If installed: after 24 hours, the status file and logs show the recorder alive, with measured MB per day and gap totals.

## Out of scope (named so nobody expects it)

- Feeding the live `/scalp` flow gate (PR B, below).
- Validating one historical day against the exchange's S3 archive. This is blocked on Nyan creating an AWS account.
- The three pre-registered flow tests. Note that the wallet-flow test also needs a source of wallet profitability labels (each wallet's past results), which the recorder does not provide and nothing here builds.
- Any hosted runner (VPS or Railway). With the Mac kept awake this may never be needed, and we decide after week one.

## PR B, later and only with explicit approval

This would make the market fetcher read the recorder's tape for the execution window and mark it reliable only when the window is gap-free and fresh. That flips `/scalp` from "always WAIT on flow" to "can fire when flow agrees". It is medium-to-high risk, because it gates entries on a real-money tool. Precondition: one flow test passes out of sample, or Nyan states an explicit override in writing. It would ship as a draft PR with a second review.

## Risks

- **Silent hangs** are the main one, and step 2's watchdog plus the sleep-and-wake test address them.
- **Mac on battery or lid closed.** Keeping it awake on mains power doesn't cover battery. Those periods show as gaps, which is honest but still a hole.
- **Exchange changes since July.** The message format may have drifted, and the live run catches that.
- **Disk estimate unverified** until week one is measured.
