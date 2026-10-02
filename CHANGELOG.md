# Changelog — scalp skill

## 2026-10-03 — liquidity briefing on the Railway watcher
New `market_brief.py`, run by `railway_watch.py` on its own thread. It sends a
market-liquidity briefing to the Telegram chat at 08:00 and 20:30 Singapore
time, sends a one-off alert when funding is extreme (hourly, at or beyond
+-0.005%), open interest drops 5% or more within an hour (BTC, ETH, HYPE), or a
high-impact USD event is within 60 minutes (each at most once per 4 hours),
and answers `/liquidity` from the configured chat. `/check` is unchanged.
Free sources only (Hyperliquid public market reads, DefiLlama, FRED, Yahoo,
SoSoValue, ForexFactory); a failed source shows as "Unavailable: <source>"
and never stops the rest. Read-only: no order, no account read, and
`railway_watch.py`'s docstring now says the briefing adds public market reads.
Optional env vars: `BRIEF_ENABLED` (set false to turn it off), `BRIEF_TIMES`,
`BRIEF_COINS`, `BRIEF_FUNDING_ALERT_PCT`, `BRIEF_OI_DROP_PCT`,
`BRIEF_EVENT_MINUTES`, set to the same defaults as plain values in
`.railway/railway.ts`. Order-book depth within 1% comes from the finest
aggregated `l2Book` (`nSigFigs` 4, 3, 2) that reaches the band; the spread
from the full-precision book. `/liquidity` never blocks `/check` (a fresh
briefing is built on its own thread), fresh `/liquidity` data also runs the
alerts, all sources share a 20-second cap, the 1-hour OI change ignores a
baseline from before a sampling gap, and every slot over 30 minutes late is
skipped. Sources share one fixed pool of 16 threads and a source still
running from an earlier call is not called again (cross-review fixes,
rounds 1-2). Conditions, not signals. Plan:
`PLAN-liquidity-briefing-2026-10-03.md`.

## 2026-09-29 — recorder disk-space warning floor is configurable
The recorder warned "disk low" every 30 seconds because its fixed 10 GB
floor is larger than the 5 GB Railway volume. `RECORDER_DISK_FLOOR_GB` now
sets the floor (default still 10); `.railway/railway.ts` sets it to 2 for the
recorder service. Warning only — nothing is ever deleted because of it.

## 2026-09-29 — Telegram `/check` on the watcher
The watcher now answers `/check` from the configured Telegram chat (and no
other) with its latest account reading: entry allowed or blocked, daily
budget left, open stop risk, equity, stop coverage and notes. The
allowed/blocked call goes through `account_monitor.watcher_report_problem`,
the gate `remote-check` itself now uses (extracted unchanged from
`remote_check`), so Telegram and `/scalp` give the same answer for the same
reading. Read-only; no extra account read; commands sent while the watcher
was down are dropped.

## 2026-09-28 — Railway settings moved to `.railway/railway.ts`
The repo-root `railway.json` (the watcher's settings) was also applied to the
new recorder service, so the recorder built the watcher's image and failed
its health check. Both services' build, start, health-check and
replica settings now live in one Railway infrastructure-as-code file,
`.railway/railway.ts`, applied with `railway config apply`; `railway.json`
and `railway.recorder.json` are removed (Config as Code is deprecated and
stops working 2026-12-01). Secrets stay in Railway (`preserve()`); the
watcher's `RECORDER_STATUS_URL` now points at the recorder's private domain
by reference.

## 2026-09-28 — flow recorder as a second Railway service, uploading to a Railway Storage Bucket (PR C)
Added `railway_record.py`, a second always-on Railway service that runs
`recorder.py`'s existing WebSocket capture loop unchanged — what and how it
records is untouched — for coins from `RECORDER_COINS` (default `HYPE`),
writing to `DATA_DIR` (default `/data`). A background thread uploads each
finished (already gzip-rotated) UTC-day file to a private Railway Storage
Bucket over its S3-compatible API (`boto3`), using HEAD-before-PUT
semantics. Each PUT carries the file's SHA-256 as object metadata, and an
object counts as confirmed only when a HEAD shows both the local size and
that digest; an existing object that differs (or has no digest) is a
failure and is **never overwritten**. A small JSON ledger under `DATA_DIR`
(atomic writes) tracks confirmed uploads, bound to the endpoint and bucket,
so a restart never re-uploads and a bucket change never trusts the old
bucket's confirmations. An S3 client that can't be built disables uploads
without stopping recording, like missing config. Failed uploads retry every 10
minutes and a network or bucket problem never stops or slows recording. A
local `.gz` is deleted only once its upload is confirmed **and** its UTC day
is more than `RECORDER_KEEP_DAYS` (default 3) days past its end — an
unconfirmed file is never deleted, however old. Missing any of the five
`S3_*` env vars (`S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`,
`S3_SECRET_ACCESS_KEY`, plus optional `S3_REGION`/`S3_ADDRESSING_STYLE`)
disables uploads — a clear `/status` field, not a fatal startup error;
recording continues regardless. The S3 secret key and access key id are
never printed or logged, including inside exceptions: only the error
code/class is logged, never a raw exception message (which botocore can
populate with request details). `recorder.py` gained one small,
behaviour-preserving hook (`FlowRecorder(..., on_status=callback)`, fired
from the existing status loop) so the service's HTTP thread reads a
snapshot instead of racing the recorder's own asyncio recv loop by calling
`status()` concurrently from another thread. The service serves
`GET /health` (200, process-alive only — never tied to exchange
connectivity, so an ordinary exchange outage can't make Railway's
healthcheck restart it and manufacture a recording gap) and `GET /status`
(JSON: connected, seconds since last message, reconnects, total gap
seconds, upload enabled/last-success/oldest-unconfirmed-day/failing-since,
disk free — never a secret) on `PORT`, bound to `::` (dual-stack, since
Railway private networking is IPv6).

`railway_watch.py` gained an optional recorder health check, entirely off
by default (`RECORDER_STATUS_URL` unset = zero behaviour change). When set,
it polls the recorder's `/status` at most once a minute on a background
thread, at most one poll in flight (a poll hung for over 2 minutes reads as
unreachable), independent of the account check's own 15–30s cadence and raises two more alerts through the
same Telegram channel and appear/30-minute-repeat/all-clear machinery as the
account problems, but as a fully separate `RECORDER_*` problem group that
never touches `/report`, `entry_allowed`, or any account-derived alert, and
whose own exceptions can never crash or stall the account loop:
`RECORDER_SILENT` (status unreachable, not connected, or no message for 5
minutes, sustained 5 minutes) and `RECORDER_UPLOAD_FAILING` (uploads
disabled by missing S3 configuration, a finished day unconfirmed more than
24h after its day ended, or uploads failing for 24h).

Added `Dockerfile.recorder` (installs `websockets` + `boto3`, only this
service's two files) and `railway.recorder.json` (custom Railway config
path pointing at it). The existing `Dockerfile`/`railway.json` (the
watcher's) are unchanged — the watcher doesn't need `boto3` and imports
nothing new. Added `RECORDER-RAILWAY.md` (owner setup: create the Storage
Bucket, create the recorder service with the custom config path, attach a
volume, wire the five `S3_*` variables as references to the bucket's own
variables, point the watcher's `RECORDER_STATUS_URL` at it, and how to
list/download the tape later with the `aws` CLI against the bucket's
endpoint). `ACCOUNT-MONITOR.md` documents `RECORDER_STATUS_URL` and the two
new alerts.

Tests (`tests/test_railway_record.py`, `tests/test_railway_watch.py`
additions; no network, no real bucket — a fake S3 client and injectable
clocks throughout): upload confirmed by HEAD after PUT; an existing key with
a matching size confirms without a PUT; a mismatched size fails and is
never overwritten; an inconclusive HEAD error (not a definite not-found)
never leads to a PUT; retry succeeds once the fake client recovers; missing
S3 config disables uploads without stopping recording; never uploads the
current day's plain file or its own day's `.gz` defensively; a ledger entry
is invalidated (and re-uploads) if the local file grows past its confirmed
size (the wall-clock-stepped-back gzip-append case `recorder.py`
documents); deletion only after confirmed and past the keep window
(including the exact boundary — not yet at exactly `keep_days`, deleted
just past it); an unconfirmed file is never deleted however old; the ledger
survives a restart without any client call once already confirmed;
`/status`'s `oldest_unconfirmed_day` and `seconds_upload_failing` track
correctly across a failing-then-recovering pass; `/health` and `/status`
over the real stdlib handler (via the existing `FakeConnection` no-socket-
bind pattern); `/health` stays 200 even while disconnected; secrets never
appear in logs, the ledger file, or `/status`, including when the fake
client's exception messages themselves embed the fake secret (an
adversarial test of `_error_code`, which extracts only the botocore error
code, never the exception's message). Watcher: the recorder check is a
no-op with zero fetch calls when `RECORDER_STATUS_URL` is unset;
`RECORDER_SILENT` appears only after 5 minutes sustained and clears on a
healthy poll; silent from `connected: false` or a stale
`seconds_since_last_message`, not just unreachable; the silent timer resets
on an intervening healthy poll; `RECORDER_UPLOAD_FAILING` appears
immediately when uploads are disabled (no 24h wait, like
`CONFIG_UNSUPPORTED` for account problems) and also from a stale
unconfirmed day or a long failing streak, clearing when caught up, and left
untouched (not cleared) on an unreachable poll; recorder problems never
read or clear account-derived problems (`STOP:`/etc.) and vice versa; the
poll is gated to at most once a minute across faster account-loop
iterations and polls again once the interval elapses; a recorder-check
exception never affects the account loop's own checks/pings.

Verified end-to-end (beyond the unit tests) by starting the real
`DualStackHTTPServer` on a real socket with a real background `Uploader`
thread and a fake S3 client, then issuing real HTTP `GET` requests: `/health`
→ `200 ok`; `/status` → the finished test file shows as uploaded and
confirmed (`oldest_unconfirmed_day: null`, `uploads_enabled: true`) after
the uploader's background pass; an unknown path → `404`.

Scope: `account_risk.py`, `account_monitor.py`, the `/scalp` entry
preflight, and what/how the recorder itself records are all unchanged;
read-only toward the exchange (the recorder only subscribes to public
market data); no order placement.

## 2026-09-28 — account preflight reads the Railway watcher (PR B)
Added `account_monitor.py remote-check [--json]`, a read-only subcommand that
reads the always-on Railway watcher's `GET /report` (PR A) instead of taking
a local exchange reading. It resolves the watcher URL from env
`SCALP_WATCHER_URL` (wins) or the local config's new optional `watcher_url`
field (`configure --watcher-url`, validated as an https URL; old configs
without it keep working), and the token from env `SCALP_WATCHER_TOKEN` or the
macOS Keychain (`security find-generic-password -s
scalp-watcher-report-token -w`, via subprocess with a short timeout). Neither
the token nor the Authorization header is ever printed, logged or included in
any output, including error messages. It returns the watcher's report
unchanged in shape (the same fields `check --json` produces) plus
`source: "railway"` and `report_age_s`, and fails closed — never falling back
to a local check — on a missing URL/token (`CONFIG_REQUIRED`), an
unreachable/non-200/malformed watcher response, or a report whose
`produced_at_ms` is more than 60 seconds old or more than 5 seconds in the
future (`DATA_UNAVAILABLE`, `entry_allowed: false`, no `observation`, a plain
reason). A garbled answer also fails closed: an `entry_allowed` that is not a real
true/false, a non-finite or non-numeric `produced_at_ms`, any NaN/Infinity in
the body (including overflow such as `1e400`), a non-string configured
`watcher_url`, a response containing the token (raw or JSON-escaped), or
`entry_allowed: true` outside a complete `CLEAR` report (every invariant
`account_risk.evaluate` guarantees when it grants entry). The env URL is https-validated too, before
the token is sent, and redirects are not followed. Exit codes match `check`: 0 eligible, 3 HALT, 2 other blocked/unknown.
`SKILL.md`'s account preflight (the Step 2 entry-preflight bullet and the
`/scalp account check` admin command) now runs `remote-check --json` instead
of `check --json`; every existing preflight rule is unchanged, and the local
`check` remains documented as available to run by hand. `ACCOUNT-MONITOR.md`
documents `remote-check`, the 60-second freshness rule, fail-closed behaviour,
where the token comes from, and that the Mac's midnight-baseline launchd job
(`scripts/midnight_watch.sh` / `com.nyanyk.scalp-midnight.plist`) becomes
unnecessary once this is live and trusted — retiring it is a separate owner
action, not done here. Tests
(`tests/test_account_monitor_remote_check.py`, no network or Keychain access,
the HTTP GET and the Keychain reader both injected): a fresh report passes
`entry_allowed` through as the watcher said, including a pass-through HALT; a
stale (>60s) or future-dated (>5s) report blocks; 401 reads "refused the
token", 503 and a connection/timeout error read "unavailable"/"unreachable";
malformed JSON and a missing `produced_at_ms`/`report` field block; a missing
URL or an unresolvable token (env unset, Keychain fails) give
`CONFIG_REQUIRED`; env URL overrides config URL; an old config without
`watcher_url` and a corrupt local config both keep working; the token never
appears in the JSON or rendered output across every success/failure path, nor
in the raw Keychain-reader output; `configure --watcher-url` accepts an https
URL and rejects `http://`; CLI wiring never falls back to `check()` for
`remote-check` and its exit codes match. Scope: `account_risk.py`,
`railway_watch.py`, the recorder and the launchd job are unchanged; read-only,
no order placement.

## 2026-09-28 — always-on Railway watcher for the account monitor (PR A)
Added `railway_watch.py`, a small always-on loop that calls
`account_monitor.check()` directly (never the `watch` subcommand, never
parsed text) and sends Telegram alerts, intended to run as a Railway
service. It is strictly read-only: it cannot place, cancel or close an
order, and adds no exchange read beyond the existing `account_api.READ_TYPES`.
Polls every 30s normally, 15s from 23:58–00:02 configured-timezone time.
Derives the current set of problems from structured fields only (per-coin
stop coverage, the daily loss latch, open trigger-distance risk vs. the
remaining budget, oversized positions, underwater adds, config/unsupported
account modes, and the monitor's own read failures), alerts on appearance,
repeats every 30 minutes while active, and sends an "all clear" only from a
fully evaluated report — a failed report never clears a problem, since it
carries no positions. Underwater adds are one-shot events, alerting once per
occurrence (keyed by coin + fill time). Monitor failure needs 5 minutes of
consecutive failed checks before alerting (a single blip never fires).
`account_risk.evaluate()`'s report gained structured `oversized_positions`
and `underwater_adds` fields alongside the existing reason text (built from
the same structured items via the new `underwater_add_reason`, so the text
is unchanged); existing tests pass unchanged. A stdlib-only HTTP server
serves `GET /report` (the latest check report, Bearer-token protected,
`hmac.compare_digest`, 401 on missing/wrong token, 503 when the token is
unset — never open) and `GET /health` for Railway's own health check.
Config is applied on boot from `MONITOR_WALLET`, `MONITOR_DAILY_LOSS_USDC`,
`MONITOR_TIMEZONE`, `MONITOR_MAX_POSITION_NOTIONAL_USDC` and `DATA_DIR`,
exiting non-zero with a clear message if wallet or loss limit is missing.
`Dockerfile` (python:3.12-slim + tzdata) and `railway.json` (Dockerfile
builder, `python3 railway_watch.py`, restart on failure, one replica) added
for the Railway deploy; the recorder service is a later PR, not this one.
Tests (`tests/test_railway_watch.py`, no network): classification of
evaluated/config/failed reports including the accounting-mismatch trap
(a failed report can still carry a stale `observation`); appear/repeat/clear
for stop coverage, oversized positions, the daily halt latch and excess
open risk; a failed report never producing an all-clear; the 5-minute
failure rule including a single blip and an intervening success resetting
it; config/unsupported problems not clearing on a failed read; the
once-per-day underwater alert; the midnight fast-poll window boundaries;
Telegram/healthcheck failures logged without the token or URL and never
raising; the loop continuing after a send raises; config-from-env; and
`/report` auth (no token, wrong token, right token, unset `REPORT_TOKEN`)
exercised via a fake connection object, since this sandbox refuses real
socket binds. ACCOUNT-MONITOR.md gained a Railway watcher section.

## 2026-09-27 — account monitor: oversized-position and add-while-underwater warnings
Added two read-only WARNING reasons to `account_monitor.py`, motivated by a
replay of the owner's real fills showing losses came from oversized positions
and from adding to losing positions — the monitor already flagged missing
stops (`UNPROTECTED`) but not these. Both are reasons only: they never change
`status` or `entry_allowed` (a later, separate decision may make either
blocking). OVERSIZED: a new optional `max_position_notional_usdc` config field
(`configure --max-position-notional-usdc`, validated like the other config
fields; existing config files without it keep working) flags any open
position whose notional (mark, or entry price if no mark, × absolute size)
exceeds the cap; when the cap is not configured, one reminder reason is added
instead. ADDED-WHILE-UNDERWATER: `account_observation.underwater_adds`
reconstructs the running average entry from the current risk day's own fills
(seeded when a fill shows `startPosition == 0`, i.e. a fresh open from flat)
and flags an opening fill that added to an existing same-direction position at
a price worse than that running entry, naming the coin, UTC fill time and
price; when the day's fetched fills never show the coin flat, the live
position `entryPx` is used as a documented approximation instead. Both checks
run read-only, over data the monitor already fetches — no new endpoints
beyond the existing `READ_TYPES`. The reconstruction uses each fill's own
`startPosition` (exchange ground truth) rather than a locally accumulated
tally, and resets on every fresh open from flat, so a same-day
close-then-reopen-then-add is never compared against a stale prior entry;
spot fills are excluded the same way `accounting_events` already excludes
them. Tests (`tests/test_account_risk.py`): over cap, under cap, cap not
configured, old config without the field, underwater add long and short, a
profitable add (no warning), a fresh open from flat (no warning), a
close-then-reopen-then-profitable-add (no warning — the regression this
reconstruction guards against), a standard-mode spot fill sequence (no
warning), and that `status`/`entry_allowed` are unchanged by both warnings.
Files: `account_observation.py`, `account_risk.py`, `account_monitor.py`,
`tests/test_account_risk.py`, `ACCOUNT-MONITOR.md`. Codex cross-review
(2 rounds) then fixed three missed-warning paths: the check now covers the
whole risk day even when the baseline began mid-day (same-day fetches start
at the day's start; accounting still counts from the baseline), including a
fill stamped exactly at the start, and a flip starts an exact entry.

## 2026-09-27 — WebSocket flow recorder revived (research only, PR A)
Brought `recorder.py` + its tests + `PLAN-flow-recorder-2026-07-12.md` over from
the local-only July branch `fix/flow-honesty` onto main, per `tasks/todo.md`'s
plan: keeps all three streams (trades, l2Book, bbo), writes an append-only tape
to `.flow_data/` (gitignored), never deletes data. Does NOT feed the live
`/scalp` flow gate — `/scalp` verdicts and `flow.py`/`fetch_market.py`'s
`sample_pct`/conviction logic are unchanged; wiring the gate is a separate,
later decision (PR B).
Kept one piece of the July `sample_pct` commit: `fetch_market.fetch_candles`
now carries HL's own per-candle trade count `n`, which `recorder.verify_sample_pct`
needs for its self-check (main previously dropped this field, silently
returning 0/None). Dropped the rest of that commit (the `coverage_pct`→
`sample_pct` rename) — PR #7 solved the same honesty problem differently.
New for this revival, beyond the July branch: a receive watchdog (default 30s,
`--watchdog-s`) forces a reconnect — through the normal GAP-writing path — when
a stream goes silent, covering the half-open-socket-after-sleep case that used
to hang forever; connection errors are now logged instead of silently
swallowed. Day-rotation compression: `FlowFileSet` gzips a finished UTC day's
file in place (atomically) on rotation and on startup for any crash leftovers;
today's file always stays plain; `verify_sample_pct` and a shared reader read
both forms. A disk-floor check (default 10 GB, `--disk-floor-gb`) is
alert-only — `disk_low`/free GB in `status.json` and a log warning, never a
delete. `status.json` also now reports per-stream lag, reconnects in the last
24h and total gap seconds in the last 24h; the per-coin trade-id dedupe set
clears at each UTC day roll to bound memory over a long session (HL replays
~30s of trades on subscribe, so readers must dedupe by tid across files
regardless).
Added `scripts/flow_recorder.sh` (pinned interpreter, `exec caffeinate -s`
so launchd's SIGTERM reaches the recorder) and
`scripts/com.nyanyk.scalp-flow-recorder.plist` (RunAtLoad, KeepAlive,
30s throttle) — NOT installed; that is a separate, pending decision.
Full suite green (417 tests, up from 386 on main; 31 in `tests/test_recorder.py`),
no live network. Live acceptance: ~3-minute HYPE capture on this Mac, clean
SIGTERM shutdown, `--verify` 72/72 trades = 100.0% sample_pct over the one
whole UTC minute the capture fully spanned — matching July's 945/945. A
shorter (~2 min) first attempt returned "recorded span shorter than one whole
minute" instead of a number; that is the whole-minute-edge-trimming guard
working as designed on a very short run, not a bug.

## 2026-09-25 — one-candle rule paper backtest: no edge
Paper-tested the "one-candle rule" from a Scarface Trades video (daily trend + retest of
the last opposing 1m candle in the first NY-open hour, 2R target) on 16 months of Binance
HYPEUSDT 1m data, net of `costs.py`. Rules and pass criterion pre-registered in
`one_candle_spec.md` before running. Result: 61 trades, −0.23R/trade (CI −0.60…+0.12),
inside the random-entry placebo range; the video's candle-trail exit was worst (−0.47R);
NY hour leaned better than other hours but CIs overlap. Verdict FAIL — not adopted.
Rejected: Hyperliquid candles as the data source (only ~5000 bars ≈ 17 days of 5m).
Side-finding, NOT fixed (needs Nyan's OK): the skill's fixed 13:30–15:30 UTC US-open
window is an hour early in US winter (EST → 14:30 UTC).
Also noticed: the 2026-09-25 midnight bracket produced a partial_day baseline
(first baseline 16:07 UTC; watcher rc=2), so new entries were WARMUP-blocked all day.
Files: `one_candle_spec.md`, `one_candle_bt.py`, `one_candle_results.json` (new);
`.audit_log.jsonl` (+2 rows: HYPE long HALT, HYPE long WAIT under owner override);
`profile.json` equity 6559 → 5145.

## 2026-09-16 — midnight watcher live; risk-dashboard design (Sections 1–2 approved, 3 pending)
Account-monitor baseline is now anchored nightly: `scripts/midnight_watch.sh`
(caffeinate + `watch --interval-seconds 15 --count 30`) fired by
`~/Library/LaunchAgents/com.nyanyk.scalp-midnight.plist` at 23:55, with a
`pmset repeat wakeorpoweron MTWRFSU 23:55:00` set by Nyan on 2026-09-11.
`logs/midnight-watch.log` shows `near_reset_observation` on 09-15 and 09-16.
Two limits found the hard way: each `check` takes ~100 s (30 polls took 57 min,
not 7), so the bracket depends on the wake being on time; and an open USDC
spot order makes `evaluate()` return the old state untouched, so a resting
spot order at midnight defeats the bracket. Both are documented in
ACCOUNT-MONITOR notes / wiki.

Design-only (no code): a private risk dashboard + Telegram alerts. Decided —
collector runs on **Railway** (Hetzner VPS declined; Mac sleeps; iPhone can't
host a loop); here.now hosts the page with email-allowlist access and
**Site Data** as the mailbox (verified against live docs: no server compute,
proxy routes + account variables exist, Site Data CRUD exists); alert rules =
no stop ×2 cycles, stop vanished, budget 50%/100%, position >24 h,
liquidation within 15%, monitor blind >5 min, plus a daily summary;
Telegram is the channel. Ownership split (Section 3) still awaiting Nyan:
collector owns account state, Mac `/scalp` stays sole audit-log writer, page
writes only resolve requests. Rejected: external API + proxy route (public
endpoint returning balances), Mac-only, and Herdr (a session-persistence
runtime, not an always-on host). Opportunity pings from scan2 deferred:
0 of 107 audit rows are resolved, so there is no evidence any card pays.
Files: `scripts/midnight_watch.sh`, `scripts/com.nyanyk.scalp-midnight.plist`,
`.audit_log.jsonl` (+5 rows: PONS HALT, 3× MANAGE, 1× EXIT).

## 2026-09-08 — account monitor: unified-account mode supported
`account_monitor.py` previously reported `UNSUPPORTED` for any wallet in
Hyperliquid's `unifiedAccount` mode, because the perp clearinghouse state shows
$0 there — the USDC lives in the spot clearinghouse and perps draw margin from
it. Unified mode is now a supported scope, treated as **perps-only**: equity =
spot USDC `total` + Σ perp `unrealizedPnl` (per-dex `accountValue` is not added,
to avoid double counting); a USDC `hold` makes equity unknown; a spot fill or an
`accountClassTransfer` since the baseline blocks reconciliation instead of being
skipped. `fetch_snapshot` reads `spotClearinghouseState` only in unified mode,
so standard-mode requests are unchanged. `portfolioMargin` / `dexAbstraction` /
`default` stay unsupported. Also: `http.client.HTTPException` (e.g.
`IncompleteRead` from a truncated proxy response) is now reported as
`DATA_UNAVAILABLE` instead of escaping as a traceback. The equity definition is
guarded by the existing reconciliation identity (residual > 0.05 USDC fails
closed) and is verified live only once a small position has been opened and
closed with zero residual. 18 new tests; suite 368 → 386 green.
Files: `account_api.py`, `account_observation.py`, `ACCOUNT-MONITOR.md`,
`tests/test_account_api.py`, `tests/test_account_observation.py`,
`tests/test_account_risk.py`.
## 2026-09-01 — scalp2 correlation-aware aggregate-risk cap (#5)
A single scan could surface up to `MAX_CARDS` (3) cards, each carrying a full
per-attempt risk budget — so taking two cards on coins that move together
silently multiplied real risk on one thesis, and a long-here/short-there pair on
correlated coins was an incoherent fleet. Neither was capped. Fix: a pure
post-filter in `card2.build_cards`, inserted between the existing `-rr` sort and
the `MAX_CARDS` slice (no existing gate touched). `scan2._coin_read` now threads
a 15m close series (`closes_15m`) into each read; `_corr_clusters` computes
plain-Python log-return Pearson correlation (no numpy) and union-find connected
components, clustering coins at `|rho| ≥ CORR_THRESHOLD` (0.7) over
`CORR_WINDOW_BARS` (24) bars. Within each cluster only the highest-net-R:R card
survives; suppressed cards get a `no_trade_reasons` line naming the kept coin and
rho. Fail-open by design: a coin with <25 closes, a flat/zero-variance series, or
a missing `closes_15m` key never correlates and survives as its own singleton
(div-by-zero guarded) — correlation that can't be computed never drops a real
setup. Design choice: keep-one-per-cluster, NOT split-budget-across-legs (one
managed position beats two half-size correlated ones — same risk, double the
taker fees and management surface; matches the "cut size, never widen" ethos).
Enforces both the aggregate-risk cap and one-direction-per-correlated-fleet in a
single move. Built via build-to-pr (PR #5, reviewed PASS/medium, 2 low nits
squashed), merged to main (squash `4aca9962`). 11 new tests
(`tests/test_card2_correlation.py`); suite green.
Files: `card2.py`, `scan2.py`, `scalp2.md`, `tests/test_card2_correlation.py`.

Deferred by Nyan's choice (not built): fail-closed-if-native-stop-absent and
broker-first position reconciliation on restart — both need his Hyperliquid
wallet address (public info) in scalp2 config; reopen anytime.

## 2026-08-24 — strip-BTC gate promoted to a deterministic helper
Promoted Step 1c from a prose/model-applied gate to a deterministic code helper,
matching how flow/regime/behavioral gates work. New `strip_btc.py` (pure, mirrors
`regime.py`/`flow.py`): `classify(candles, btc_candles, corr=...)` returns
`{status, cut, cut_tiers, btc_corr, coin_move_pct, btc_move_pct, reason}`.
`status` ∈ {beta, idiosyncratic, moderate, decoupled, unavailable}; `cut` is true
only for `beta` (corr ≥ 0.7, coin move aligned with BTC and not outrunning it by
>1.5×, BTC not ~flat). Reuses `regime.btc_corr` (passed in from `out['regime']`)
so the two reads never disagree. Wired into `fetch_market.assemble` as
`out['strip_btc']`; Step 1c + SKILL.md now say "read `out['strip_btc']`, do not
re-derive by eye." Direction-neutral, CUT-only. Thresholds provisional
(`strip_btc.DEFAULT_PARAMS`). Live check on HYPE: corr 0.486 → status moderate →
no cut (correct). 13 new tests (`tests/test_strip_btc.py`); suite 265 → 278 green.
Files: `strip_btc.py` (new), `fetch_market.py`, `scalp-core.md`, `SKILL.md`,
`tests/test_strip_btc.py`.

## 2026-08-24 — Step 1c strip-BTC idiosyncrasy gate (CUT-only)
New conviction gate in `scalp-core.md`: a directional scalp whose move is pure
BTC beta (`regime.btc_corr` ≥ 0.7, same direction as BTC, coin not outrunning
BTC by >1.5×) gets conviction cut one tier; a genuine coin-specific residual
(against BTC, or beyond it) does not. Rationale: the "filter the confounder"
partial-correlation logic from the ML pairs-trading deep-read (Rotondi & Russo
2025) — don't pay directional conviction for index beta the whole market shares.
Distinct from the macro veto (which stops on BTC *danger*); this cuts on BTC
*authorship* even when macro is CLEAR. Consumes only the numeric `btc_corr`
input, so `regime_label` stays Phase-1 READ-ONLY as designed. Thresholds
(0.7 / 0.4 / 1.5×) provisional — revisit after 20 resolved trades. Documented
blind spot: `btc_corr` is Pearson (linear), so a nonlinearly-BTC-driven coin
(calm normally, dumps hard on BTC dumps) can slip the gate — macro veto is the
backstop. Wired into the final-conviction min rule (Steps 1b, 4b), QUICK output
(silence-by-default STRIP-BTC line), WAIT reason list, TINY (β-cut token), and
SKILL.md (execution checklist + frontmatter). Regression check: direction
modules carry no conviction-tier logic (rule lives only in core), so the gate is
incorporated in one place; passive mode marked informational-only. Docs-only
change — no Python touched, no test impact.
Commits `683d8aa` + `c01bb0b` on `feature/scalp-changelog-eval` (local, unpushed).
Files: `scalp-core.md`, `SKILL.md`.
Source note: vault `knowledge/concepts/partial-correlation-filters-confounders.md`.

## 2026-08-06 — post-mortem-driven behavioral gates (coin lockout, tilt-coin guard, MANAGE time-stop)
Full-fill post-mortem of the real Hyperliquid account (11,944 fills, 2025-01 →
2026-08: net −$254k, of which −$384k across three days — ETH short capitulation
2025-08-22, DOGE long liquidation 2025-10-10, DOGE re-entry unwind 2025-12-31)
showed all three blowups shared one mechanism: scalp-sized thesis → multi-day
leveraged hold with no stop → re-entry into the same losing coin+side.
Counterfactual replay on the actual fills: a −$5k daily circuit breaker saved
$425k / forfeited $0 (16/16 tripped days never recovered); a same-coin+side
lockout blocked a −$346k pattern. Encoded: `behavioral.py` `coin_lockout`
(2 losses ≤ −0.5R same coin+side within 12h → 24h hard ENTRY block; MANAGE
proceeds; passive fades excluded), scalp-core Step 0e tilt-coin guard (DOGE:
lifetime −$228.5k → conviction capped at med, in-plan only, until 20 resolved
DOGE trades show positive expectancy), Step M position-age time-stop (>24h =
forced close-or-swing decision; >72h underwater = stale-hold callout), and the
0a daily stop pinned with its empirical basis. Additive dict key — card2.py /
decide.py consumers read via `.get()`, unaffected. 6 new tests; suite 265
green. Known limit (recorded in lessons): gates see only skill-logged trades;
real-fill API integration listed as an open item.
Files: behavioral.py, scalp-core.md, SKILL.md, tests/test_behavioral.py.
Post-mortem: vault raw/hyperliquid-perp-post-mortem-2025-2026.md.

## 2026-07-31 — NO-TRADE lines name the binding macro condition
A vetoed coin printed only `macro veto blocks <lean>`; the causes lived in
`macro["flags"]`, which `build_cards` forwarded on the behavioral-cooldown and
session-stop branches but dropped on the per-coin veto branch — so a live
NO-TRADE couldn't be read without reconstructing the gate by hand (hit during a
manual `/scalp2 HYPE` where the cause turned out to be BTC 1h breakdown).
`macro_gate` now returns `veto_reasons: {"long": [...], "short": [...]}` and
derives `veto_long`/`veto_short` from those lists, so state and text can't
drift; the veto branch appends the matching side's causes. Incidental: the four
`flags` strings are generated from one source instead of duplicated, and the
BTC funding flag now says "BTC funding" — it reads BTC's funding but said just
"funding", colliding with the per-coin `own funding` veto message below it.
`build_cards` reads the key via `.get()`, so a macro dict without it degrades
to the old bare line rather than raising. Full suite 259 passed; ruff unchanged
at 9 pre-existing errors (lambda assignments in tests). Verified live: HYPE
scan now prints `macro veto blocks long — BTC 1h structural breakdown`.
Files: card2.py, tests/test_card2_gates.py.

## 2026-07-30 — regression eval harness for decide() (PR #2, merged)
Added `evals/` — a golden-file regression suite for the deterministic decision
engine. `fetch_market.py` now appends every run's full market dict to
`evals/snapshots.jsonl` (gitignored firehose; file-only sink, stdout untouched,
cannot break a live fetch). `evals/bless.py` freezes a snapshot's current
`decide()` output as a committed golden case; `evals/run_regression.py` replays
`golden/*.json` through the current `decide()` and deep-diffs vs frozen
`expected` (ignores human-text `reason`), non-zero exit on mismatch. Wired into
pytest via `tests/test_evals.py` (+ `tests/fixtures/market_hype.json`): 4 tests
— bless roundtrip, fresh-set passes, corrupted verdict fails, reason ignored.
Full suite 259 passed (255 + 4). Files: fetch_market.py, .gitignore, evals/*,
tests/test_evals.py, tests/fixtures/market_hype.json.
KEY FINDING: no fetch→decide adapter needed — `fetch_market` output is a direct
superset of decide()'s market contract (a live spike disproved an earlier grep
that suggested `btc_ctx`/`macro_can_clear`/`btc_dominance` were absent). Scope:
regression-tests the deterministic engine (the port of the LLM's logic), NOT the
live interactive /scalp LLM verdict — that's a separate future eval. Golden set
is curated, not exhaustive; seeded with 2 real HYPE WAIT cases, fills as varied
snapshots accrue and get blessed. Merged via feature-branch PR (merge commit
15eaa9c); local main fast-forwarded.

## 2026-07-27 — scalp2 merged to main + activated
Branch feat/scalp2-scanner merged (8 commits, 255 tests). Final Opus
whole-branch review caught 3 cross-module defects fixed pre-merge:
funding veto was wired to BTC not the traded coin; session-stop/evidence
gates had no ENTRY writer (added `scan2.py log-entry`); single-coin fetch
failure aborted whole scan (now per-coin isolated). Activated with user
approval: crontab 20:55 SGT weekdays + skill symlink
`.claude/skills/scalp2` → `skill/scalp2`. Cron pinned to Python 3.14
after system 3.9 reproducibly failed on HL responses (IncompleteRead);
verified under cron-equivalent minimal env. Equity $5,000 confirmed;
fees base tier (user-confirmed). Live checkout moved
fix/flow-honesty → main (scan2.py must be on the checked-out branch for
cron; WIP branches preserved).

## 2026-07-26 — scalp2 scanner (feat/scalp2-scanner)
Session-open scanner per spec 2026-07-24: universe2/card2/scan2 + thin
/scalp2 skill. Scanner claims no entry alpha (deterministic cards, human
decides); reuses validated v1 gates. Risk 0.5%/attempt proving-phase,
evidence gate at 40 resolved. Cron 20:55 SGT weekdays (pending user approval).
v1 skill untouched. Rejected: greenfield rewrite (re-pays hardened
lessons); strip-v1-in-place (can't reach lightweight by subtraction).

## 2026-07-10 — Deterministic live-bot investigation → SHELVED (no edge)

**Summary:** Investigated turning the skill into an unattended deterministic
bot (VPS loop + Telegram alerts, no LLM in the decision path, alert-only).
Built the full engine + backtest harness, then the go/no-go gate returned a
robust NO-GO: the triggers have no generalizable edge. Not deployed.

- **New modules:** `structure.py` (fractal pivots → ATR-clustered tested
  levels + sweep flags), `triggers.py` (the 4 setups as pure functions with
  gross/net R:R + geometry guards; close-vs-retest entry variants),
  `decide.py` (behavioral → macro veto → structure/triggers → flow cuts →
  sizing → verdict), `backtest.py` (walk-forward + intraday 15m-levels/5m-entry
  same-day path, reusing `replay.py`'s fill simulator; param-sweep + noise
  lenses). `structure.with_entry_sweeps` shares the two-timeframe logic so
  backtest == live. +37 tests → 222 green.
- **Result:** every trigger net-negative to breakeven OOS across HYPE/SOL/INJ/
  NEAR, both entry modes. The one apparent lead (HYPE long_A +0.40R) was a
  stop-placement bug artifact — collapsed to n1/−1.40R once fixed.
- **External review** corrected 3 port mis-specs (long_B ceilings-only; long_A
  structural-pool stop or skip; retest entries); the rerun *strengthened* the
  veto (commit `889d733`).
- **Decision:** SHELVE the live bot. Engine + harness + methodology retained as
  reusable tooling. The skill's value is its discipline/gating layer, not entry
  alpha (unfiltered trading loses ~10× more than the gated system).
- **Docs:** `PLAN-live-bot-2026-07-09.md`, `FINDINGS-live-bot-2026-07-10.md`
  (reviewer-facing), `analysis_rr_sweep.py`, `analysis_intraday_oos.py`.
- **Branch:** `feat/deterministic-live-bot` (PR #1) — merged to main 2026-07-10
  (merge `4ea2ca0`) as reference tooling; not wired into the live `/scalp`
  skill, alert-only, never executes orders.

## 2026-07-07 — VWAP + OI context (READ-ONLY Phase 1)

**Summary:** The two top deferred-backlog items from the July reviews, landed
read-only per the regime-classifier precedent — context only, zero
verdict/conviction effect until backtested.

- **VWAP:** `compute_vwap()` — deterministic UTC-day-anchored VWAP (1h
  hlc3×volume) in `out['vwap']` with side + dev_bps. Shown on the Range line;
  direction modules flag `fighting VWAP` entries as context. Passive mode's
  mean is now this value instead of LLM-computed prose.
- **OI:** new rolling `.oi_cache.jsonl` (btcd-cache pattern — Hyperliquid only
  exposes current OI). `out['oi']` carries 1h/24h OI + window-aligned price
  change and a classic OI×price read (new-longs / short-covering / new-shorts /
  long-unwind / flat; noise thresholds provisional). Direction modules document
  the doctrine per side. `read: null` = cache warming.
- **Tests:** VWAP/OI unit tests + assemble isolation for the new cache write +
  consistency-lint guards. Suite green at 169 passing, no network.
- **Files:** fetch_market.py, scalp-core.md, scalp-long.md, scalp-short.md,
  scalp-passive.md, SKILL.md, .gitignore, tests.

## 2026-07-06 — Hardening: daily stop, net R:R, spread/depth guard

**Summary:** Closed the top risk gaps from the July reviews.

- **Test isolation:** factored core-perps `metaAndAssetCtxs` into
  `fetch_core_meta()` so assemble tests patch the network boundary instead of
  hitting live Hyperliquid DNS.
- **Daily stop:** `behavioral.py` now emits `daily_stop`, active on trailing
  24h realized R ≤ -2.0R or two consecutive directional losses ≤ -0.7R. The
  skill treats it as a hard ENTRY `HALT (daily stop)` while MANAGE remains
  allowed. Backtest against the current audit log found only 1 resolved trade,
  so thresholds are conservative and not yet tunable.
- **Net R:R:** added shared `costs.py`; replay imports the same fee/slippage
  constants as live admission. Directional trigger admission is now net of
  trading costs, and audit trigger payloads include `net_rr_t1` / `net_rr_t2`.
- **Spread/depth:** fetcher now exposes top-3 L2 depth. Step 6e adds provisional
  cost-heavy execution rules and a 25% top-3 depth size cap.
- **Tests:** full suite green at 160 passing.

## 2026-06-29 — Noise-injection robustness test (Varma §7)

**Summary:** Added the input-noise robustness lens to `backtest_thresholds.py`
— the complement to the existing parameter sweeps. A sweep asks "is the
threshold on a plateau?"; this asks "is the edge even real?" by perturbing input
prices with rising random noise and checking the edge metric degrades smoothly.

- **`backtest_thresholds.py`** — `add_noise` (per-bar multiplicative O/H/L/C
  noise, seeded/deterministic), `noise_robustness` (curve of edge vs sigma,
  reps-averaged, with per-level std), `degradation_verdict` (PASS = smooth
  decay toward 0; SUSPECT = jagged / strengthens under noise), and two edge
  metrics: `atr_protective_edge`, `regime_separation_edge`. New `--noise` CLI.
- **Tests** — +7 (`test_backtest.py`): identity at sigma 0, H/L bracket
  preserved, determinism, curve shape, PASS/SUSPECT/INSUFFICIENT verdicts.
  **158 passed.**
- **Live run (HYPE, 365d, 5000 1h bars, 8 reps):**
  - **regime gate** — separation real & stable at low noise (+0.0058,
    std ~0.0004 at sigma≤0.001); SUSPECT flag is driven by a high-noise reading
    within ~1 std (measurement variance, not a true spike). Read: **likely
    robust, borderline** — don't curve-fit `compression_quiet`; more reps to
    confirm.
  - **ATR veto (long side)** — base effect tiny and WRONG-SIGNED (-0.0017):
    **no robust long-protective edge on HYPE.** Actionable: the long-side ATR
    veto isn't earning its keep here (may only protect shorts / needs rework).

## 2026-06-28 — Volume / order-flow confirmation gate (Step 1b)

**Summary:** Encoded Varma's "volume is the key signal" heuristics
([[samir-varma-react-to-risk-quant-trading]] §8a) as a deterministic classifier
+ a conviction gate. Volume now has to CONFIRM a move or conviction is cut.

- **`flow.py`** (new, mirrors `regime.py` — pure, never raises): from candles
  (per-bar volume) + bucketed `taker_delta` it emits `coverage_ok`,
  `aggressor_bias`, `volume_climax` (capitulation/blow-off), `delta_divergence`
  (price-up-on-selling / price-down-on-buying = exhaustion), `breakout_vol_ok`.
  Climax/breakout baselines **adapt to available bars** (fetch returns ~19 5m
  bars) down to a `min_bars` floor.
- **`fetch_market.py`** — attaches `out['flow']` next to `out['regime']`.
- **`scalp-core.md` Step 1b** (new) — maps the flow read to conviction; it only
  ever **cuts** (volume confirms an edge or it doesn't, never manufactures one):
  low coverage → cap low; bias opposing the side → −2; balanced → −1; divergence
  against side → −1; climax in your direction (you're late) → −1; unconfirmed
  breakout → −1. Cuts stack; drives WAIT when low + weak macro. New FLOW-GATE
  output line; optional FLOW line on WAITs. SKILL.md coverage note updated.
- **Tests** — +11 (`test_flow.py`), incl. short-history adaptation. **151 passed.**
- Live smoke test: gate computes end-to-end on HYPE.

## 2026-06-28 — Realistic-cost counterfactuals (replay net of fees + slippage)

**Summary:** Counterfactual replay now scores trades **net of trading costs**,
closing the one gap that made expectancy untrustworthy (it previously filled at
trigger price with zero cost — flagged in the [[samir-varma-react-to-risk-quant-trading]]
review, Varma: "your system is signal + entry + exit — model all three").

- **`replay.py`** — added a cost model: `TAKER_FEE` (0.045%) + `SLIPPAGE_FRAC`
  (2 bps) per fill, `ROUND_TRIP_FILLS=2.0`, and `round_trip_cost_r(lv)` which
  converts cost to R as `2 × COST_PER_FILL × entry/risk` — so **tighter stops
  cost more in R**. `simulate_trigger` now returns `net_r` + `cost_r` alongside
  gross `r` (gross is unchanged, so the fill-logic unit tests still pin it).
  `apply_replay_to_entry` adds `net_best_r` + a `cost_model` block.
- **`audit_log.py`** — `_counterfactual_stats` aggregates NET (`net_best_r` /
  per-trigger `net_r`) with fallback to gross for pre-cost counterfactuals.
- **Tests** — +6 (cost math, net-of-cost outcomes, unfilled pays nothing,
  summary prefers net). Full suite **140 passed**.
- **Re-scored the live log (`--force`):** 47 scored. Net result — cost drag
  **0.108R/trade avg** (median 0.097R, max 0.197R), eating **~5R of ~24.5R
  gross (≈20%)**. Edge survives in simulation (long-B +1.03R/n15, long-A
  +0.70R/n13) but this is COUNTERFACTUAL, net, still optimistic on *whether*
  limit fills happen — only **1 real resolved trade** exists. Prove with live
  resolves before sizing up. Tune `TAKER_FEE`/`SLIPPAGE_FRAC` to real tier.

## 2026-06-18 — Passive flow-capture mode (Plan 2: 2A + 2B + 2C)

**Summary:** Built `/scalp passive <COIN>` — a both-sides mean-reversion fade
gated by the Plan 1 regime read and an audit-log expectancy check. Ships at
UNPROVEN (quarter-size) until ≥40 resolved passive trades prove a net edge
(else STOP). Three committed phases:

- **2A (`57111d9`)** accounting/risk foundation: `by_setup_family` summary
  breakout; `passive_expectancy()` UNPROVEN/PROVEN/STOP gate (min_samples=40,
  quarter-size until proven) + CLI; behavioral carve-out (passive excluded from
  directional FOMO) + `passive_tilt` guard.
- **2B (`04f342b`)** `scalp-passive.md` trade module (regime/expectancy/event/
  tilt gates → range edges → bid/ask limit ladders → exit-to-mean → 3-way
  invalidation → expectancy-gated sizing + session shot-cap) + `/scalp passive`
  routing.
- **2C (this commit)** right-sized: replay SKIPS passive-fade entries (they
  resolve manually; the directional ladder sim would mis-score them). The full
  passive simulator was dropped as over-engineering — see PLAN-phase2 §2C.
- **fetch fix (found by running `/scalp passive` once):** the 15m candle window
  was 2.5h (~10 bars), but the regime classifier needs ≥20 (compression) / ≥32
  (btc_corr) — so production regime was ALWAYS "unknown" and passive `fade_ok`
  NEVER armed (the feature was inert). Widened the primary + BTC 15m windows to
  12h (~48 bars) in `fetch_market.py`. Live `/scalp passive HYPE` now returns a
  real regime read (e.g. quiet → SIT-OUT, EXPECTANCY UNPROVEN ×0.25).

Full suite 136 passing. No directional behavior changed.

**Caveat:** the §7 gate was validated on 1h candles; production computes
compression/correlation on 15m. The compression sweep was inert (label is
directionality-driven), so the threshold transfers, but the 15m structural read
is not independently backtested — the expectancy gate remains the real arbiter.

**Decisions:**
- Expectancy bar raised 20→40 samples (a ~75% breakeven win rate makes 20 too
  noisy to trust a PROVEN flip).
- Edge-proximity left as an LLM prose rule, not code (floor/ceiling are
  judgment); add a helper only if soak shows drift.
- Live edge remains UNPROVEN by design — the gate must earn PROVEN on real
  resolved trades; it may instead resolve to STOP (a cheap, correct kill).

## 2026-06-17 — Regime classifier foundation (passive-mode Plan 1) + §7 gate

**Summary:** Built the deterministic market-regime read (the missing "directional
edge is poor in quiet/correlated tape" half of the order-flow thesis) as Plan 1
of a passive flow-capture mode. Ships READ-ONLY: it surfaces a WEATHER line but
does not change any verdict yet. Backtest-validated as the gate before Plan 2
(the passive-fade trade machinery) is built.

**Files (all changes uncommitted/working-tree — commits held by operator):**
- `regime.py` (new) — pure classifier: `true_ranges`, `range_compression`,
  `directionality`, `btc_corr`, `two_sided_flow`, `classify` → `regime_label`
  ∈ {trending, ranging, quiet, correlated-chop, unknown} + coarse `fade_ok`.
- `fetch_market.py` — fetch BTC 15m candles (for correlation); attach `out["regime"]`.
- `scalp-core.md` — read-only WEATHER line in QUICK + TINY output templates.
- `backtest_thresholds.py` — `regime_forward_stats` + `regime_compression_sweep`
  + `--regime` CLI (neutral-flow, trailing-window structural validation).
- `tests/test_regime.py` (new, 16) + additions to `test_fetch_market.py`,
  `test_skill_consistency.py`, `test_backtest.py`. 123 tests, all passing.
- `tests/test_fetch_market.py` — isolated `test_assemble_marks_macro_unavailable...`
  from the real on-disk BTC.D cache (operator chose test-isolation over fixing
  the latent negative-age fallback bug in uncommitted btcd code).

**§7 GATE RESULT — PASS (qualified).** `--regime --coin HYPE --days 180` (4321×1h):
- What it shows: ranging-labelled tape has LOW forward drift (trending
  mean_abs_fwd 0.0215 vs ranging 0.0149; signed drift +0.0068 vs +0.0012). This
  proves the NECESSARY-not-sufficient half — "ranging tape doesn't trend, so a
  fade won't get steamrolled." It does NOT prove fades are profitable: low drift
  ≠ mean-reversion (a random walk also has ~0 net drift). The backtest grouped
  by regime label and measured forward-return magnitude; it never conditioned on
  being AT an edge and measured return-to-mean (the actual fade edge). That edge
  is unprovable from candles (no flow history) — the live expectancy gate is the
  real arbiter.
- Compression sweep 0.4→0.8: separation flat at ~0.0066 (robust plateau, not a
  spike) — but compression_quiet is nearly INERT; directionality does the work.
- CAVEAT: structural gate is permissive — 80% of bars label "ranging" (n=3436)
  vs trending 2% (n=98). Real selectivity must come from the un-backtested FLOW
  gate (tsf) + Plan 2's edge-proximity fade_ok + the expectancy/min-size gate.
  The classifier separates regimes correctly; it is not by itself selective.

**Decision:** Gate criteria met → Plan 2 is justified. But live edge stays
UNPROVEN until the audit log earns it (by design). Flow validation is live-only
(no multi-day taker history), so the backtest validates structure only.

## 2026-06-10 — Counterfactual replay engine + threshold backtest

**Summary:** Closed the audit-log feedback loop. WAIT/VETO decisions are now
scored against what subsequently happened (replay engine), and the mechanical
macro-veto thresholds were backtested against ~208 days of HYPE history.

**Files:**
- `replay.py` (new) — counterfactual simulator: fill/stop/T1-breakeven/T2
  ladder, short mirror via price negation, same-candle conflict drill-down
  to 1m with conservative fallback, chunked HL candle fetch, CLI.
- `backtest_thresholds.py` (new) — component backtest of ATR-spike and
  funding-extreme veto conditions vs forward returns/MAE, threshold sweeps.
- `audit_log.py` — `attach_counterfactual`, GATE VALUE summary section,
  open-entry semantics exclude counterfactually-closed non-action rows.
- `SKILL.md` — `/scalp replay` admin command, GATE VALUE summary block.
- `tests/test_replay.py`, `tests/test_backtest.py` (new) — 29 tests
  (82 total, all passing).

**Decisions:**
- Forward test + counterfactual scoring is the evaluation methodology;
  classic backtest only for mechanical sub-components (signal layer is
  judgment + live-only data).
- Replay conventions: fill at trigger price, 50% T1 → BE stop, 50% T2,
  conservative ambiguity resolution, 72h window.
- Findings: funding-extreme veto validated (protective from 0.03%/8h);
  ATR down-bar veto refuted as drift signal (marks MAE risk, not
  continuation); WAITs missed +2.5R, VETOs blocked +4.1R across the first
  13 logged decisions. Threshold changes proposed, NOT yet applied to
  protocol files.

Commits: `167aa02`, `fc5a386`.

## 2026-06-11 — Threshold changes applied (Nyan sign-off)

- Funding-extreme veto tightened ±0.05 → ±0.03%/8h (both direction modules
  + SKILL.md), per backtest: protective effect clear from 0.03.
- BTC 2× ATR spike demoted from hard veto to size modifier (half size,
  wider structural stop, tighter time-box) in both modules: backtest showed
  it marks adverse-excursion risk, not directional continuation.
- Files: scalp-long.md, scalp-short.md, SKILL.md.

## 2026-06-13 — Profile/state layer + instruction-consistency lint

Acted on external review (5 of 8 items; #6 deferred, #7 rejected).

- **profile.py** (new) — `.scalp_profile.json` {equity, phase} + `/scalp
  profile` command. Sizing math now sources equity/cap deterministically;
  unblocks /loop (no more asking for equity every run).
- **behavioral.py** (new) — cooldown / FOMO-streak / OOP-this-week derived
  from the audit log instead of conversation memory. Step 0 preflight reads
  it; works headless under /loop.
- **audit payload** — `plan_status` captured so the weekly OOP cap persists.
- **SKILL.md / scalp-core.md** — frontmatter drift fixed (five admin
  commands; replay/profile/quick in triggers); journal-stub contradiction
  resolved (log all QUICK, show stub on action verdicts only).
- **tests/test_skill_consistency.py** (new) — lints command/frontmatter
  consistency + no blanket journal-stub mandate; catches the drift class.
- 22 new tests (104 total).

Deferred: econ-dates file (#6). Rejected: openai.yaml (#7, wrong ecosystem).
