# 0003. The recorder becomes a live input to the flow gate; fail closed

- **Status:** Accepted (approved by Nyan 2026-10-04)
- **Date:** 2026-10-04

## Context

At base revision `b41791c`, the flow gate (Step 1b, `flow.py`) read only the
REST `recentTrades` sample, capped at 10 trades per call. That sample can never
prove coverage, so `coverage_ok` was always false and live action verdicts were
blocked by design. The Railway recorder (`recorder.py`, `railway_record.py`)
already captures the complete trade tape, but was documented as research-only
and did not feed the gate.

## Decision

Let the gate read the recorder's tape, with no new signal, indicator or
threshold (`flow.DEFAULT_PARAMS` unchanged; the gate stays cut-only).

- The recorder serves per-minute taker rows and its gap records at `GET /flow`;
  the watcher passes them on behind its existing bearer token; `fetch_market.py`
  builds the usual windows with `source: "ws_recorder"`. All gate logic stays on
  the Mac side; Railway only reads and aggregates its own files.
- Coverage is recorded trades divided by the exchange's own per-candle trade
  count over whole closed minutes. A window is `reliable` only when the capture
  is complete (no gap in the window, recorder connected, every compared minute
  has an exchange count), coverage is at least 50% and the last trade is at most
  60 seconds old.
- Fail closed. Recorder unreachable or stale, a gap in the window, missing
  exchange counts, or a coin the recorder does not capture gives exactly the
  old REST buckets (`reliable` false, `coverage_pct` null). The REST sample is
  never treated as confirmation. Unknown stays null, never 0.
- Nothing here places, cancels or closes an order; `/report` and
  `entry_allowed` are untouched.

## Consequences

A standing block is removed: with a complete tape, `coverage_ok` can be true
and live action verdicts become possible again, on entry logic that is still
unvalidated. The 58/42 buyer/seller thresholds have never run against a
complete tape. The recorder's HTTP side keeps an incremental tail cache per
coin (`recorder.FlowTailCache`, its own lock, nothing shared with the recording
loop): the first request after a restart parses the day's file once (measured
about 1 s for 300,000 trades on a laptop; Railway not yet measured), later
requests read only the bytes appended since, plus the finished day's `.gz` once
across UTC midnight. Memory is proportional to the trades in the 4 h window
(about 26 MB for 300,000 on a laptop). The pure `aggregate_flow` full read
remains the reference, and a test asserts the cache equals it. The tape is
judged on the recorder's own clock (`now_ms` in the body), guarded against our
clock by 60 s. Only the 5m, 15m and 1h windows get coverage (the last hour of
1m candles, fetched in chunks of at most 50 bars); the 4h window keeps its sums
but is never `reliable`. The lookup is opt-in (`assemble(use_tape=True)`, passed
by the `/scalp` command-line entry point only) so the scan2 scanner makes no
extra network calls. BTC joins the recorder (`HYPE,ZEC,PUMP,BTC`); it is the
busiest market, so check the volume after its first full day. A quiet minute with no
exchange candle counts as a missing count and makes the window unreliable, a
deliberately conservative choice that may bite thinly traded coins such as
PUMP. Merging redeploys both Railway services.

After deploy, check: `/scalp HYPE` shows a real buyer/seller bias with coverage
above 50%; a coin the recorder does not capture still says flow unavailable;
stopping the recorder makes HYPE fall back to WAIT.
