# PLAN — Standalone WebSocket flow recorder (2026-07-12)

Origin: the `coverage_pct` honesty fix (same branch, `fix/flow-honesty`)
exposed that fetch_market.py's taker_delta cache is structurally sparse —
`recentTrades` caps at ~10 trades/call, polled every ~5m. That's fine for the
live /scalp loop but useless for real flow research. This recorder replaces
polling with a held-open WebSocket connection that writes a complete,
honestly-gapped tape.

## Dependency

`recorder.py` needs the `websockets` package (pure-Python asyncio WebSocket
client; not stdlib). Verified already present in this environment:

```
$ python3 -c "import websockets; print(websockets.__version__)"
16.0
```

No install was performed by this task — the package was already available
system-wide. If it's ever missing: `pip install websockets` (no other repo
dependency changed; there is no requirements.txt in this repo to update).
The pure helper functions (`iso_utc_ms`, `day_str_utc`, `make_data_record`,
`make_gap_record`, `dedupe_trade`, `FlowFileSet`, `build_status`,
`verify_sample_pct`) import cleanly without `websockets` — only
`FlowRecorder.run()` imports it, lazily, so the test suite (which mocks the
clock/stream and never opens a socket) doesn't need it either.

## Design

- One WS connection to `wss://api.hyperliquid.xyz/ws`, subscribed per coin
  (`--coins`, default `HYPE`) to `trades`, `l2Book`, `bbo`.
- Output: `.flow_data/<COIN>_<channel>_<YYYY-MM-DD>.jsonl` (gitignored),
  append-only, flushed on every write. Rotation keys off UTC day of the
  *local receive* timestamp, never exchange time (this repo has a
  timezone-bug history — exchange time is not guaranteed monotonic/available,
  receive time always is).
- Every data record: `exchange_ts_ms`/`exchange_ts_utc` (server) AND
  `recv_ts_ms`/`recv_ts_utc` (local), both via `iso_utc_ms()` — the same
  "always tz-aware, never machine-local" pattern as `fetch_market.iso_utc()`.
- Trades deduped by `tid` within the running session (an in-memory set per
  coin — not persisted across restarts, unlike fetch_market's on-disk trade
  cache). `users` [buyer, seller] is stored verbatim inside `data` — untouched,
  for future wallet-flow work.
- Reconnect: exponential backoff (1s → 60s cap). On every (re)connect —
  including the very first, cold-start connect — a GAP record is written into
  every open (coin, channel) file, carrying `last_seen_exchange_ts_ms` (None
  on cold start) and `reconnect_ts_ms`. Downstream analysis sees the hole
  explicitly instead of a silently-spanned tape.
- Status file `.flow_data/status.json`, rewritten every `--status-interval-min`
  (default 5): per-(coin,channel) message counts + a lag estimate (now minus
  last receive).
- SIGINT/SIGTERM: an `asyncio.Event` set from the signal handler unwinds the
  recv loop via a `_StopRequested` exception, closing the socket, cancelling
  the ping/status tasks, flushing and closing every open file.
- `--verify`: reads the recorder's own trades JSONL for the trailing window,
  fetches Hyperliquid's 5m candles for the same window via
  `fetch_market.fetch_candles`, sums `n` (HL's own trade count), and reports
  `sample_pct = recorded / candle_n_sum * 100` — the exact same honest metric
  as `bucket_taker_delta`'s `sample_pct`, applied to the recorder's own tape
  as its acceptance test.

## Status

- [x] `recorder.py` implemented (pure helpers + `FlowRecorder` + CLI).
- [x] Unit tests (`tests/test_recorder.py`): dedupe, gap-record emission
      (cold-start + post-data), UTC stamping (tz-independence regression),
      file rotation at UTC day boundary, message dispatch routing,
      `--verify` sample_pct computation. All mock the clock/stream — no live
      network in the test suite.
- [x] `.flow_data/` added to `.gitignore`.
- [ ] Live smoke test (~10 min) + `--verify` run — results appended below.

## Live smoke test results

See CHANGELOG / builder report for the run: duration, trades/min captured,
achieved `sample_pct`, and any GAP records logged during the run.
