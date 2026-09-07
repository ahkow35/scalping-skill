# Read-only account risk monitor

This is a persistent warning latch and an entry preflight, **not an exchange-enforced circuit breaker**. It cannot place stops, cancel resting orders, close positions, or prevent manual trading. No private key or trading credential is accepted. A profitable strategy is not established by this tool.

## Configuration and operation

Use the actual public trading wallet, not an agent wallet, and a loss limit explicitly selected by the account owner. There is no default loss amount. From this repository:

```sh
python3 account_monitor.py configure --wallet 0xYOUR_PUBLIC_WALLET --daily-loss-usdc YOUR_LIMIT
python3 account_monitor.py check --json
python3 account_monitor.py watch --interval-seconds 30
```

Alternatively configure `--daily-loss-pct YOUR_PERCENT`. Reset timezone defaults to `Asia/Singapore`; `--timezone` accepts an IANA timezone. Configuration and persistent per-wallet state live in ignored `.account_monitor/`. Do not delete state to clear a loss warning. Reconfiguration does not clear a same-day latch or increase that day's budget. No background service or notification subscription is installed automatically. Watch output is local only.

Exit codes: 0 means eligible for further skill checks, 3 means a latched HALT, 2 means another blocked/unknown state. CLEAR is not an entry signal.

## Accounting and scope

The monitor reads the exchange's states, fills, funding, ledger and open orders, including manually originated activity in the monitored scope. It supports explicitly standard (`disabled` abstraction) USDC perpetual balances across advertised perp venues. It does **not** sum unified/portfolio-margin balances: those modes, unspecified modes, and active non-USDC collateral are unsupported and block entries. Spot, vault investments and other wallet/subaccount addresses are outside this version's scope. Never switch account modes merely to make this monitor pass.

Daily observed P&L is equity change minus net external cash flows, reconciled against closed P&L minus fees plus funding plus the change in unrealized P&L. Fees already include builder fees. Unexplained differences above 0.05 USDC, unhandled ledger events, stale observations, saturated history that cannot be paginated, and data errors block entries. Exchange history retention and non-atomic endpoint snapshots mean this is not a complete historical accounting archive.

First use mid-day creates a **partial-day** baseline and stays WARMUP: it cannot know losses before observation. A baseline within 60 seconds of reset is labelled near-reset, not exact midnight equity. A preceding observation within 60 seconds is retained over rollover to avoid discarding the intervening loss. Keep the monitor running across reset. HALT persists through recoveries, restarts and unavailable data; a new risk day may establish a new baseline. Missing/corrupt state is not reliable evidence of an unbreached day. Disk failures can prevent persistence and must be repaired before relying on subsequent checks.

## Protective-order audit

Every open position must have sufficient remaining quantity in active, opposite-side, reduce-only stop orders, with triggers before liquidation and on the protective side of the observed mark. Parent-order children, duplicate IDs, original rather than remaining quantity, take profits, and unknown/dynamic zero sizes cannot manufacture coverage. Incomplete coverage blocks entries. Existing open trigger-distance risk exceeding the remaining daily budget also blocks entries; this estimate excludes execution costs.

Coverage is **not** guaranteed execution: stops can slip, stop-limits can remain unfilled, and liquidation can precede an observation. Pending entry orders remain live even under HALT. The owner must separately place and verify appropriate exchange-side stops and stop opening manual trades when warned; this implementation does neither.

Source contracts: [Hyperliquid information endpoints](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint), [account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes), and [take-profit/stop-loss orders](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl).
