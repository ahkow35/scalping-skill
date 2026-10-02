# Liquidity briefing on the Railway watcher (plan, 2026-10-03)

Status: APPROVED by Nyan 2026-10-03; built in PR #20 (draft, not merged or deployed).

## Objective

Send a short market-liquidity briefing to the existing Telegram chat twice a day, send a one-off alert when a liquidity condition turns extreme, and answer a `/liquidity` command on demand. It runs inside the existing `scalping-skill` Railway service (`railway_watch.py`) and uses only free data sources, all of which were tested working on 2026-10-03.

**What it is not:** it is not an entry signal. It describes conditions (how deep the market is, how crowded positioning is, what events are coming), not direction. Our backtests found no edge from signals of this kind, so every message ends with "Conditions, not signals." It never places, cancels or closes an order and never reads the account.

## Decisions (defaults, open for Nyan to change at approval)

| # | Decision | Default | Why |
|---|---|---|---|
| 1 | Schedule | **08:00 and 20:30 Asia/Singapore** | 08:00 recaps the US overnight session. 20:30 lands before the US stock open in both seasons (21:30 SGT now, 22:30 SGT after 1 Nov). |
| 2 | Coins covered | **BTC, ETH, HYPE** (env `BRIEF_COINS`) | Your most traded coins. Changeable without a code change. |
| 3 | Alert: extreme funding | Hourly funding **≥ +0.005% or ≤ −0.005%** (about ±44% a year) | Starting guess, not tuned. Today's BTC value is 0.00125%. |
| 4 | Alert: leverage flush | Open interest **drops ≥ 5% within 1 hour** on a covered coin | Starting guess. A sharp OI drop means forced closing just happened. |
| 5 | Alert: event soon | A **high-impact USD event within 60 minutes** (ForexFactory) | CPI, FOMC and jobs data thin the book before the release. |
| 6 | Alert repeats | Each condition alerts **at most once every 4 hours** | Keeps alerts from crowding out account alerts on the same chat. |
| 7 | Delivery | **One PR** via `build-to-pr`, draft (medium risk) | Merge redeploys the live watcher (see Risks). |

## Data sources (all free, no keys; tested 2026-10-03)

| Section | Source | Endpoint | Refresh |
|---|---|---|---|
| Funding, open interest, 24h volume, mark price | Hyperliquid | `POST /info {"type":"metaAndAssetCtxs"}` | Live |
| Order-book depth within ±1% and the spread | Hyperliquid | `POST /info {"type":"l2Book"}` | Live |
| Stablecoin supply (USDT, USDC), day and week change | DefiLlama | `stablecoins.llama.fi/stablecoins` (has `circulatingPrevDay/Week`) | Daily |
| Fed net liquidity = balance sheet − Treasury cash account − reverse repo | FRED CSV | `fredgraph.csv?id=WALCL`, `WTREGEN`, `RRPONTSYD` (units: millions, millions, **billions**) | Weekly / daily |
| VIX, dollar index, US 10-year yield | Yahoo (unofficial) | `query1.finance.yahoo.com/v8/finance/chart/{^VIX,DX-Y.NYB,^TNX}` | Live |
| BTC spot ETF net flow | SoSoValue (unofficial, key-free access undocumented) | `POST api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart {"type":"us-btc-spot"}` | Daily |
| Economic calendar | ForexFactory feed | `nfs.faireconomy.media/ff_calendar_thisweek.json` | Weekly file |

Left out on purpose: CoinGlass (paid), Coinalyze (needs a key), Farside (bot-blocked), Binance/Bybit/OKX (not needed for Hyperliquid coins; revisit with the CoinGlass decision after ~2 weeks of use).

## Message shape (plain text, under Telegram's 4,000-character cap)

```
Liquidity briefing — Sat 3 Oct, 08:00 SGT

MACRO
Fed net liquidity  $5.71T  (−$29B w/w)
Dollar index 98.1 (+0.2%)  ·  10y 4.12% (−3bp)  ·  VIX 15.8 (−0.9)

CRYPTO MONEY
Stablecoins $296.4B  (+$0.8B w/w)
BTC ETF flow (1 Oct)  +$103M

HYPERLIQUID          funding/h   OI 24h    ±1% depth   spread
BTC                  0.0013%     +2.1%     $8.4M       0.4bp
ETH ...
HYPE ...

NEXT 24H EVENTS
21:30  USD  Non-Farm Payrolls (high)

Unavailable: none
Conditions, not signals.
```

(Numbers above are placeholders for layout only.)

## Build

1. **New module `market_brief.py`.** One small fetch function per source, each with an injectable HTTP function and a short timeout. Each source fails on its own: a failure becomes an "Unavailable: <source>" line and never stops the rest. Pure functions compute the figures and format the message. A `--print` command-line mode prints a live briefing locally for checking.
2. **Open-interest history.** Hyperliquid has no OI history endpoint, so the module samples OI every 15 minutes and keeps 25 hours of samples in a small JSON file under `DATA_DIR`. That file survives restarts. The 24h OI change and the 1-hour flush alert both read from it. Until 1 hour (or 24 hours) of samples exists, the figure shows "warming up".
3. **Scheduler thread in `railway_watch.py`.** A daemon thread, separate from the account loop, so a slow or failing data source can never delay an account alert. It:
   - sends the briefing at each scheduled time;
   - keeps a "last sent" marker per slot in `DATA_DIR`, so a restart neither double-sends nor resends an old slot. If the service was down at slot time and comes back within 30 minutes, it sends late, marked "(late)"; after 30 minutes it skips that slot;
   - runs the alert checks every 15 minutes (same pass as the OI sample), with the 4-hour repeat limit per condition kept in the same marker file.
4. **`/liquidity` command.** Extend `TelegramCommands` to dispatch `/check` and `/liquidity`, same configured-chat-only rule. It reuses a briefing at most 5 minutes old, otherwise builds a fresh one. It shares the existing per-chat reply rate limit.
5. **Messages go through the existing `Outbox`.** Every briefing carries its "as of" time in the first line, so one delivered late after a Telegram outage is visibly stale.
6. **Config via env, all optional:** `BRIEF_ENABLED` (default on), `BRIEF_TIMES` (`08:00,20:30`), `BRIEF_COINS` (`BTC,ETH,HYPE`), plus the alert thresholds from decisions 3–5. No new secrets. Add the new names to `.railway/railway.ts` and `ACCOUNT-MONITOR.md`.
7. **`Dockerfile`:** add `market_brief.py` to the `COPY` line. No new Python packages (`requests` and the standard `csv` module cover it).
8. **Docs:** update the `railway_watch.py` module docstring. It currently promises "no exchange read beyond account_monitor.py's own calls". The briefing adds **public market reads** (`metaAndAssetCtxs`, `l2Book`), never account reads, and the docstring must say so. Add a section to `ACCOUNT-MONITOR.md` and a `CHANGELOG.md` entry.

## Tests (no network in tests)

- Save today's real responses from each source as fixtures under `tests/fixtures/market_brief/`.
- `tests/test_market_brief.py`: parsing for each source; the net-liquidity unit conversion (RRP is in billions); a source raising or returning junk produces an "Unavailable" line while the others still render; message length under 4,000; ±1% depth and spread maths on a fixed book.
- Scheduler: fires once per slot; no double-send across a simulated restart; late-send inside 30 minutes, skip after; alert repeat limit; OI warming-up state.
- Commands: `/liquidity` answered only for the configured chat; `/check` behaviour unchanged (existing tests must still pass).
- Full suite: `python3 -m pytest tests/`.

## Verification

1. Locally: `python3 market_brief.py --print` against live sources, read by eye against the provider websites for BTC funding, VIX and stablecoin supply.
2. After merge and redeploy (**needs Nyan's OK, since merge redeploys the live watcher**): send `/liquidity` and `/check` in Telegram, confirm both answer, then confirm the next scheduled briefing arrives on time. Check the Railway logs for any "Unavailable" source. That is the first proof the sources work from Railway's Singapore servers rather than the Mac.

## Risks

- **Merge = redeploy of the live account watcher (medium risk).** A crash at startup would also stop the account alerts. Mitigations: the briefing is isolated in its own thread with all errors caught; `BRIEF_ENABLED=false` turns it off without a code change; the dead-man check already alerts if the watcher stops.
- **Unofficial sources** (Yahoo, SoSoValue) can break or start demanding keys without notice. Each degrades to an "Unavailable" line; nothing fails silently.
- **Thresholds are guesses.** Expect to tune decisions 3–4 after the first week or two.
- **Read as signals.** The footer and this plan say it plainly: these describe conditions only.

## Out of scope

Paid data (CoinGlass), cross-exchange aggregation, charts or images, any change to `/scalp`'s entry logic, and any trading action.

## Checklist

- [ ] Nyan approves the plan and the defaults table
- [ ] `git pull` main, then `build-to-pr` (draft PR)
- [ ] Review the PR; local `--print` check
- [ ] Nyan's OK to merge, which redeploys Railway
- [ ] Post-deploy: `/liquidity`, `/check`, first scheduled briefing, logs clean
- [ ] After ~2 weeks: tune thresholds; decide CoinGlass / Coinalyze
