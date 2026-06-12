# Changelog — scalp skill

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
