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

---

# PENDING WIKI UPDATES (apply when iCloud vault access returns)

The Obsidian vault under iCloud was inaccessible when this work shipped
(macOS "Operation not permitted" on the whole vault — dataless-file eviction
/ Full Disk Access). The three blocks below are ready to paste. After fixing
access (open the vault in Obsidian/Finder to re-materialize), apply each, then
delete this PENDING section.

## 1 → projects/scalp-skill.md

REPLACE the existing "## Open / future" section with the two sections below
(the new "State layer" section goes immediately ABOVE a rewritten Open/future):

```markdown
## State layer (added 2026-06-13)

External review (8 items) actioned — 5 built, 1 deferred, 1 rejected, 1
reframed:

- **`profile.py`** (`/scalp profile`): `.scalp_profile.json` holds equity +
  phase. Sizing math (Step 6a) now sources equity deterministically — the
  gap that made `/loop` unusable (it had to ask for equity every run).
- **`behavioral.py`**: cooldown (last resolved loss >0.7R within 24h), FOMO
  cooldown (last 2 entries both leaked → 3d), `fomo_streak`, `oop_this_week`
  — all derived from the audit log, not conversation memory. Step 0 preflight
  reads it; runs headless. (Live check 2026-06-13: `fomo_streak` = 5 — the
  last five entries all leaked, confirming the behavioral degradation.)
- **`plan_status`** captured in the audit payload so the weekly OOP cap holds
  across `/loop` runs.
- **Instruction-drift fixes**: frontmatter now lists five admin commands +
  replay/profile/quick triggers; journal-stub contradiction resolved (log all
  QUICK, show stub on action verdicts only). Guarded by
  `tests/test_skill_consistency.py` (a lint, not an LLM eval — the realistic
  form of the reviewer's "golden tests").
- Reviewer's design note corrected: cooldown/FOMO/OOP did NOT need a new
  state store (would duplicate the audit log) — derived from it instead. New
  store is only for what the log can't know (equity/phase).

## Open / future

- BTC.D veto condition remains untestable historically (no free dominance
  series) — only the live rolling cache accumulates evidence.
- Deferred from the review: econ-dates file (local `econ_dates.json` for
  FOMC/CPI/NFP/PCE → `event_day` in fetch_market; chosen over a live
  calendar API to keep the no-paid-API/deterministic design). Rejected:
  `agents/openai.yaml` (wrong ecosystem — this is a Claude Code skill).
- Still open: audit_log schema validation (drift observed live: `YELLOW`
  macro status, `med-high` conviction, stub triggers), Saturday-plan
  artifact for verifiable R16 Levels grading.
```

## 2 → system/lessons/trading.md

INSERT these two bullets at the top of the lessons list (most recent first):

```markdown
- **2026-06-13 (derive behavioral state from the log you already keep):** The
  scalp Step 0 preflight relied on conversation memory for cooldown/FOMO/OOP —
  which is empty under `/loop`. Fix: derive all three from the existing audit
  log (`behavioral.py`), not a new parallel store (which would duplicate the
  log and drift from it). Only persist what the log genuinely can't know
  (equity, phase) in a separate profile file. Pattern: before building a state
  store, check whether the data already lives in a log you're writing anyway.

- **2026-06-13 (a "safety gate" can bundle a helpful and a harmful rule):**
  The macro veto bundled a funding-extreme condition (validated: protective)
  with a BTC ATR-spike condition (refuted: marked capitulation bounces, cost
  upside). Test each mechanically-testable gate condition in isolation before
  trusting the bundle. Instruction drift is real too — frontmatter, body, and
  output specs silently diverged; a deterministic consistency lint
  (`test_skill_consistency.py`) catches that class far cheaper than an LLM eval.
```

## 3 → raw/sessions/2026-06-13-scalp-state-layer.md (new file)

```markdown
# Session: 2026-06-13 — Scalp state layer + review follow-ups

## Decisions made
- Actioned external review of the scalp skill: 5 of 8 items built, 1 deferred
  (econ-dates file), 1 rejected (openai.yaml — wrong ecosystem), 1 reframed
  (#8 golden tests → deterministic consistency lint).
- Behavioral state derived from the audit log, NOT a new store; profile file
  only for equity/phase.

## Projects updated
- projects/scalp-skill.md — new "State layer" section + Open/future rewrite.
- Repo /Users/nyanyk/Claude/research/scalp commit 1bb8ac4 (local-only),
  104/104 tests. New: profile.py, behavioral.py, tests/test_profile.py,
  tests/test_behavioral.py, tests/test_skill_consistency.py. Modified:
  audit_log payload (plan_status), SKILL.md, scalp-core.md, .gitignore.

## New information learned
- behavioral.py on the real log: fomo_streak = 5 (last five entries all
  leaked) — quantifies the behavioral degradation flagged 2026-06-10.
- Funding-extreme veto validated, ATR-spike veto refuted (applied 2026-06-11).

## Predicted regressions
- Step 0/6a now shell out to behavioral.py / profile.py — if either script is
  missing/errors, the preflight must fail loud, not silently skip the gate.
- profile equity unset → action verdicts must refuse to size, not guess.
- list-open/summary "open" semantics already changed 2026-06-10 (cf-closed
  non-action rows excluded).

## Regression check
- 104/104 tests pass; profile + behavioral CLIs verified on real data;
  consistency lint green. iCloud vault write BLOCKED mid-session (OS-level),
  so wiki updates staged in repo CHANGELOG instead — this file included.

## Open questions
- Build deferred items (econ-dates file, audit schema validation,
  Saturday-plan artifact)?
- Will Nyan run /loop during planned windows? (still the biggest lever)
```
