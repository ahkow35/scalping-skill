# scalp evals — regression harness for the decision engine

Catches silent regressions when you edit `decide.py`, `structure.py`,
`triggers.py`, `costs.py`, or their thresholds. Freezes the engine's current
verdict on a set of real market snapshots and flags any later divergence.

## Pieces

- **Recorder** — `fetch_market.py` appends every run's full market dict to
  `evals/snapshots.jsonl` (gitignored firehose). Real inputs accrue as scalp runs.
- **`bless.py`** — freezes a snapshot into a committed golden case:
  runs `decide()` now, stores `{market, side, behavioral, profile, expected}`.
- **`run_regression.py`** — replays every `golden/*.json` through the CURRENT
  `decide()` and diffs vs the frozen `expected`. Exits non-zero on any mismatch.
  Runs in the pytest suite via `tests/test_evals.py`.

## Workflow

```bash
# 1. real snapshots accumulate automatically as you run /scalp
# 2. bless a representative one (or a fixture) into the golden set
python3 evals/bless.py --latest                 # newest snapshot, both sides
# 3. commit golden/*.json  -> that behaviour is now locked in
# 4. after editing decision code:
python3 evals/run_regression.py
```

A diff is one of two things:
- a **bug** — fix the code until the regression passes again; or
- an **intended change** — re-bless the affected cases and commit, noting *why*
  in the commit message. Re-blessing is the deliberate act of moving the baseline.

## Scope / caveats

- Regression-tests the **deterministic engine** `decide()`, which is the port of
  the LLM's logic (see `decide.py` docstring). It does **not** test the live
  interactive `/scalp` verdict (that's the LLM reasoning through the markdown).
  LLM-vs-engine divergence is a separate future eval.
- The golden set is **curated, not exhaustive** — bless cases that cover the
  branches you care about (veto / wait / action / low-coverage, long & short).
  Coverage gaps are silent; add cases as new behaviours appear.
- Behavioural state and profile are frozen into each case for determinism
  (default: clear gate, $5000 / phase 1).
