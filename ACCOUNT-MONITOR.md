# Read-only account risk monitor

This is a persistent warning latch and an entry preflight, **not an exchange-enforced circuit breaker**. It cannot place stops, cancel resting orders, close positions, or prevent manual trading. No private key or trading credential is accepted. A profitable strategy is not established by this tool.

## Configuration and operation

Use the actual public trading wallet, not an agent wallet, and a loss limit explicitly selected by the account owner. There is no default loss amount. From this repository:

```sh
python3 account_monitor.py configure --wallet 0xYOUR_PUBLIC_WALLET --daily-loss-usdc YOUR_LIMIT \
  --watcher-url https://YOUR-RAILWAY-WATCHER.up.railway.app
python3 account_monitor.py check --json
python3 account_monitor.py remote-check --json
python3 account_monitor.py watch --interval-seconds 30
```

Alternatively configure `--daily-loss-pct YOUR_PERCENT`. Reset timezone defaults to `Asia/Singapore`; `--timezone` accepts an IANA timezone. Optionally configure `--max-position-notional-usdc YOUR_CAP` (an owner-selected per-position size warning; there is no default) and `--watcher-url` (see "Remote preflight" below; also optional). `configure` always rewrites the whole saved config: any of these optional flags left off a given `configure` call resets to unconfigured for that call, the same way `--max-position-notional-usdc` already behaves — pass `--watcher-url` again on every reconfigure, or it drops. Configuration and persistent per-wallet state live in ignored `.account_monitor/`. Do not delete state to clear a loss warning. Reconfiguration does not clear a same-day latch or increase that day's budget. No background service or notification subscription is installed automatically. Watch output is local only.

Exit codes: 0 means eligible for further skill checks, 3 means a latched HALT, 2 means another blocked/unknown state. CLEAR is not an entry signal.

## Accounting and scope

The monitor reads the exchange's states, fills, funding, ledger and open orders, including manually originated activity in the monitored scope. Two account modes are supported:

- **Standard** (`disabled` abstraction): equity is the sum of USDC perpetual balances across advertised perp venues. Spot is a separate balance and is ignored, including spot fills.
- **Unified account** (`unifiedAccount`): equity is the spot-clearinghouse USDC balance plus perp unrealized P&L. Per-dex perp `accountValue` is deliberately not added (it is not a separate balance). The account is treated as **perps-only**: a USDC `hold` (open spot order) makes equity unknown, and any spot fill or `accountClassTransfer` observed since the baseline blocks reconciliation for the day. Other spot tokens never count as equity. The spot state carries no exchange timestamp, so spot/perp read coherence is not checkable; a race between the two reads shows up as a reconciliation residual and fails closed.

Portfolio margin, unspecified modes, and active non-USDC collateral are unsupported and block entries. Vault investments and other wallet/subaccount addresses are outside scope. Never switch account modes merely to make this monitor pass.

Daily observed P&L is equity change minus net external cash flows, reconciled against closed P&L minus fees plus funding plus the change in unrealized P&L. Fees already include builder fees. Unexplained differences above 0.05 USDC, unhandled ledger events, stale observations, saturated history that cannot be paginated, and data errors block entries. Exchange history retention and non-atomic endpoint snapshots mean this is not a complete historical accounting archive.

First use creates a **partial-day** baseline and stays WARMUP, even just after midnight: it cannot know losses before observation. Only a rollover with observations within 60 seconds on both sides establishes a near-reset baseline, retaining the preceding observation to avoid discarding the intervening loss. This is not exact midnight equity. Keep the monitor running across reset. HALT persists through recoveries, restarts and unavailable data; a new risk day may establish a new baseline. Missing/corrupt state is not reliable evidence of an unbreached day. Disk failures can prevent persistence and must be repaired before relying on subsequent checks.

## Protective-order audit

Every open position must have sufficient remaining quantity in active, opposite-side, reduce-only stop orders, with triggers before liquidation and on the protective side of the observed mark. Parent-order children, duplicate IDs, original rather than remaining quantity, take profits, and unknown/dynamic zero sizes cannot manufacture coverage. Incomplete coverage blocks entries. Existing open trigger-distance risk exceeding the remaining daily budget also blocks entries; this estimate excludes execution costs.

Coverage is **not** guaranteed execution: stops can slip, stop-limits can remain unfilled, and liquidation can precede an observation. Pending entry orders remain live even under HALT. The owner must separately place and verify appropriate exchange-side stops and stop opening manual trades when warned; this implementation does neither.

## Sizing and add-while-underwater warnings

Two additional checks add reasons only — they never change `status` or `entry_allowed`, and cannot block or permit anything by themselves:

- **Oversized position**: if `--max-position-notional-usdc` is configured, any open position whose notional (mark price, or entry price if no mark is available, times absolute size) exceeds it adds a reason naming the coin, its notional and the cap. If the cap is not configured, every check adds a reason saying so instead — this is a reminder, not a warning about your positions.
- **Added while underwater**: from the current risk day's fills (fetched from the day's start even when the baseline began mid-day), an opening fill that increased an existing same-direction position (a same-sign, nonzero prior position) at a price worse than that position's entry at the time adds a reason naming the coin, the fill time (UTC) and price. The running entry is reconstructed from the day's own fills when they show the coin starting flat or flipping direction; otherwise the live `entryPx` is used as an approximation (documented in code), since it reflects entry as of now, not as of that specific historical fill.

Both are read-only warnings surfaced only in `reasons`; a later, separate decision may choose to make either blocking.

## Railway watcher

`railway_watch.py` runs this monitor as an always-on Railway service. It calls `account_monitor.check()` directly in a loop — every 30 seconds normally, every 15 seconds from 23:58 to 00:02 configured-timezone time so the watcher's own day baseline lands as a near-reset observation — and sends a Telegram alert when a problem appears (unprotected/partial stop coverage, the daily loss latch, open trigger-distance risk exceeding the remaining budget, an oversized position, an add to a losing position, or the monitor itself failing to read the account for over 5 minutes), repeating every 30 minutes while it lasts, with an "all clear" when a fully successful check shows it gone. It is read-only in the same sense as the rest of this file: it cannot place, cancel or close an order, and adds no exchange read beyond `account_api.READ_TYPES`.

Environment variables it reads on boot (names only — values are Railway service secrets, never pasted into chat or committed):

- `MONITOR_WALLET`, `MONITOR_DAILY_LOSS_USDC` — required; the watcher exits with a clear error and a non-zero status if either is missing.
- `MONITOR_TIMEZONE` — optional, defaults to `Asia/Singapore`.
- `MONITOR_MAX_POSITION_NOTIONAL_USDC` — optional oversized-position cap.
- `DATA_DIR` — the Railway volume path for persistent state; without it the state lives next to the script and does not survive a redeploy.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — the alert channel.
- `HEALTHCHECK_PING_URL` — optional dead-man check, pinged after every loop pass that produced a report (good or failed).
- `REPORT_TOKEN` — required to open `GET /report`; if unset that endpoint always returns 503 rather than serving without auth.
- `PORT` — the port the small HTTP server listens on (`GET /report`, token-protected; `GET /health`, unauthenticated, for Railway's own health check; nothing else is served).
- `RECORDER_STATUS_URL` — optional; the flow recorder's second Railway service (RECORDER-RAILWAY.md), e.g. `http://<recorder-service>.railway.internal:8080/status`. Unset (the default) means the recorder check is entirely off — zero behaviour change to anything above.

### Recorder alerts (optional, `RECORDER_STATUS_URL`)

When `RECORDER_STATUS_URL` is set, the watcher additionally polls it (at most once a minute, independent of the account check's own cadence) and raises two more alerts through the same Telegram channel and appear/30-minute-repeat/all-clear rules as the account problems above — but as a fully separate group that never affects `/report`, `entry_allowed`, or any account-derived alert, and a fetch exception here can never crash or stall the account loop:

- `RECORDER_SILENT` — the recorder's `/status` was unreachable, or reported not connected, or no message for 5 minutes, sustained for 5 minutes. Clears only when a poll comes back healthy.
- `RECORDER_UPLOAD_FAILING` — uploads disabled by missing S3 bucket configuration, or a finished day's file is still unconfirmed uploaded to the Railway Storage Bucket more than 24 hours after that UTC day ended, or uploads have been failing for over 24 hours. Clears when caught up.

**Merging to `main` redeploys the watcher** once Railway follows the main branch — a normal `git push` to a feature branch does not. **Never merge or deploy within 10 minutes of midnight Singapore time**: a restart across the boundary leaves the watcher's own day baseline partial for that day. `/report` is token-protected (`hmac.compare_digest` against `REPORT_TOKEN`, sent as `Authorization: Bearer <token>`); it is on Railway's public domain, so treat the token like any other credential.

## Remote preflight (`remote-check`)

`python3 account_monitor.py remote-check --json` reads the watcher's `/report`
instead of taking a local exchange reading, and is what `/scalp`'s account
preflight and `/scalp account check` now run. It returns the watcher's report
unchanged in shape (the same fields `check --json` produces, so it reads
identically) plus `source: "railway"` and `report_age_s`. The local `python3
account_monitor.py check` still works and remains available to run by hand.

It resolves the watcher URL from `SCALP_WATCHER_URL` if set (env wins), else
the local config's optional `watcher_url` field (`configure --watcher-url
YOUR_HTTPS_URL`, validated as an https URL; a config saved before this field
existed keeps working — it just has no watcher URL to read). Either source
must be an https URL, or the token is never sent (`CONFIG_REQUIRED`);
redirects are not followed. The token comes
from `SCALP_WATCHER_TOKEN` if set, else the macOS Keychain (`security
find-generic-password -s scalp-watcher-report-token -w`). Neither the token
nor the Authorization header is ever printed, logged, or included in any
error message.

It **fails closed** and never falls back to a local check: a missing URL or
token, an unreachable or non-200 watcher, a malformed body, or a report whose
`produced_at_ms` is more than **60 seconds** old or more than 5 seconds in
the future all produce `status: DATA_UNAVAILABLE` (or `CONFIG_REQUIRED` for a
missing URL/token), `entry_allowed: false`, and no `observation` — a plain
reason names why (e.g. a 401 reads "watcher refused the token"). A garbled
answer blocks too: `entry_allowed` that is not a real true/false, a broken
`produced_at_ms`, any NaN, Infinity or overflowing number, a response that
contains the token (raw or JSON-escaped), or permission outside a complete
`CLEAR` report: status `CLEAR`, not latched, a `near_reset_observation`
(full-day) baseline, positive equity, P&L inside the limit, a remaining budget
equal to limit + P&L, and known open risk that fits it. Exit codes
match `check`: 0 eligible, 3 HALT, 2 other blocked/unknown.

Once this is live and trusted, the Mac's `scripts/midnight_watch.sh` /
`com.nyanyk.scalp-midnight.plist` local nightly-baseline job becomes
unnecessary — the watcher's own always-on loop already anchors its baseline
across every midnight. Retiring that launchd job is a separate owner action,
not done by this change.

Source contracts: [Hyperliquid information endpoints](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint), [account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes), and [take-profit/stop-loss orders](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl).
