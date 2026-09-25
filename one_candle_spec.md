# One-candle rule — paper backtest spec (pre-registered 2026-09-25)

Source: Scarface Trades, "Once You Master Scalping…" (YouTube CXeCT3nJYms, 2026-09-08).
Vault note: `raw/2026-09-25-scarface-trades-one-candle-rule-scalping.md`.
Paper-only. Rules were fixed BEFORE any result was seen; all variants are reported.

## Data
- Binance USDT-M perp HYPEUSDT 1m klines (data.binance.vision), 2025-05-30 → last published day.
  Prices are Binance's; costs use the /scalp Hyperliquid model (`costs.py`: 0.045% taker +
  0.02% slippage per fill, 2 fills). Funding ignored (holds < 2.5h).
- Download: `https://data.binance.vision/data/futures/um/monthly/klines/HYPEUSDT/1m/HYPEUSDT-1m-YYYY-MM.zip`
  for 2025-05 → 2026-08, plus `.../daily/klines/HYPEUSDT/1m/HYPEUSDT-1m-2026-09-DD.zip` for 09-01 → 09-23.
- Split for robustness: H1 = before 2026-01-01, H2 = from 2026-01-01.

## Rules (the video, made mechanical)
1. **Daily trend** — UTC daily bars strictly before the session date. Swing pivots with 2 bars
   each side (confirmed only once both right-side bars closed). Last two swing highs AND last two
   swing lows both rising = up (longs only); both falling = down (shorts only); else = no trade.
2. **Session** — NY open 09:30 America/New_York (DST-aware). Entries allowed only in the first
   60 minutes; everything flat at session start + 150 min (12:00 ET).
   Intraday confirmation: price must be ≥ 0.3% beyond the session open in the trade direction
   when the setup arms.
3. **Reference candle** (long; short mirrors) — among CLOSED session bars, the red (down-close)
   candle with the highest body top. Body = [close, open]. It arms once a later bar closes above
   the body top. Entry = limit at body top, filled when a later bar trades down to it (if that
   bar opens below body top but above the stop, fill at the open). A new higher red candle
   replaces the reference and disarms it.
4. **Stop** = body bottom − 0.02%. Minimum stop distance 0.25% of price (keeps modeled costs ≤ ~0.5R).
5. One trade per session. Same-bar stop/target conflict = stop first, flagged ambiguous.

## Exits (compared)
- **fixed2R** — target = entry + 2 × risk.
- **candle-trail** (the video's step 4) — after entry, each newly closed red candle whose body
  bottom is above the current stop and below the close raises the stop to that body bottom − 0.02%.
- **swing15-trail** (current /scalp MANAGE rule) — stop raised to below the latest confirmed
  15m swing low (2 bars each side) − 0.05%, when higher than the current stop.

## Variants (all reported)
| id | window | trend filter | exit | timeframe |
|---|---|---|---|---|
| V1 | NY open | on | fixed2R | 1m |
| V2 | NY open | on | candle-trail | 1m |
| V3 | NY open | on | swing15-trail | 1m |
| V4 | NY open | off (direction = intraday move) | fixed2R | 1m |
| V5 | control: every UTC hour ≥ 3h away from NY open | on | fixed2R | 1m |
| V6 | control | on | candle-trail | 1m |
| V7 | placebo: NY open, trend on, random entry bar in window, same stop %, fixed2R (200 seeds) | on | fixed2R | 1m |
| V8 | NY open | on | fixed2R | 5m |

## Metrics
n, win rate, mean net R, median net R, total net R, 95% CI of mean (bootstrap resampled by day),
mean cost in R, ambiguous count, per-half mean.

## Pass criterion (decided before running)
The rule has an edge only if a NY-open variant (V1–V3) shows ALL of: n ≥ 100, mean net R > 0 with
the CI's lower bound > 0, positive mean in both halves, AND a mean above both the control (V5) and
the placebo (V7) 95% range. Otherwise it is filed as a negative result, like bandit.

## Results (run 2026-09-25, data 2025-05-30 → 2026-09-23, 693,450 1m bars, 0 missing minutes)
Daily trend: 177 up days, 140 down, 165 no-trend. Raw output: `one_candle_results.json`.

| id | n | win % | mean net R | 95% CI | H1 / H2 mean R |
|---|---|---|---|---|---|
| V1 NY, trend, fixed2R | 61 | 37.7 | −0.23 | −0.60 … +0.12 | +0.18 / −0.56 |
| V2 NY, trend, candle-trail | 61 | 31.1 | −0.47 | −0.78 … −0.15 | −0.23 / −0.65 |
| V3 NY, trend, swing15-trail | 61 | 14.8 | −0.44 | −0.98 … +0.24 | +0.09 / −0.86 |
| V4 NY, no trend filter, fixed2R | 154 | 38.3 | −0.21 | −0.44 … +0.01 | −0.28 / −0.14 |
| V5 control hours, trend, fixed2R | 364 | 30.2 | −0.48 | −0.61 … −0.34 | −0.46 / −0.49 |
| V6 control hours, candle-trail | 364 | 25.0 | −0.43 | −0.61 … −0.25 | −0.24 / −0.62 |
| V7 placebo (random entry, 200 seeds) | 61/seed | — | −0.16 | −0.53 … +0.21 (range of means) | — |
| V8 NY, trend, fixed2R, 5m | 37 | 48.6 | +0.19 | −0.31 … +0.70 | +0.30 / +0.13 |

Mean modeled cost ≈ 0.36R per trade (median stop 0.35%). Zero same-bar ambiguous exits.

**Verdict: FAIL — no edge found.** Strongest finding (high confidence): V1 sits inside the
random-entry placebo range, so the candle adds nothing over "enter anywhere in the window with the
same stop". Moderate: the video's own trail (V2) is the worst exit, CI fully below 0. No NY-open
variant meets the criterion; n ≥ 100 was unreachable by design (~226 eligible trend-day NY sessions
in 16 months × ~27% fill rate ≈ 61 trades). Low confidence / within noise: the NY window leans
better than control hours (−0.23 vs −0.48), consistent with the March "timing, not the candle"
hypothesis, but the CIs overlap — and it is negative after costs either way.
V8 (5m) is positive but on 37 trades with a CI spanning −0.31 to +0.70: noise, not a finding.
Side note: /scalp's fixed 13:30–15:30 UTC open window is an hour early in US winter (EST → 14:30 UTC).
