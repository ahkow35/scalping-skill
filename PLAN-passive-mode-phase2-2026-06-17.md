# Passive Mode — Plan 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the passive flow-capture trade mode on top of the Plan 1 regime classifier — gated by an audit-log-driven expectancy check that refuses to size up until a real net edge is proven.

**Architecture:** Phase 2A builds the accounting/risk foundation (setup_family tagging, the PROVEN/UNPROVEN expectancy gate, behavioral carve-out + tilt guard) — this is the real risk control, because the Plan 1 §7 gate showed the structural classifier is permissive (80% "ranging"); selectivity must come from flow + edge-proximity + expectancy, not the label alone. Phase 2B adds the `scalp-passive.md` protocol module, the trade-grade edge-proximity `fade_ok`, expectancy-gated sizing, and `/scalp passive` invocation. Phase 2C adds replay simulation of passive exits.

**Tech Stack:** Python 3 stdlib, pytest, existing scalp repo conventions. Builds on committed Plan 1 (`regime.py`, `out["regime"]`).

**Sequencing rationale:** 2A is detailed in full here and ships independently (the expectancy gate + accounting are testable with no trade logic). 2B and 2C are specified at task level and get full per-step code authored once 2A is committed — same staged pattern Plan 1 validated.

---

## Phase 2A — Accounting & expectancy foundation (DETAILED)

### File Structure (Phase 2A)
| File | Responsibility | Change |
|---|---|---|
| `audit_log.py` | `by_setup_family` summary breakout; `passive_expectancy()` gate | Modify |
| `behavioral.py` | exclude passive-fade from FOMO logic; `passive_tilt` guard | Modify |
| `tests/test_audit_log.py` | tests for breakout + expectancy gate | Modify |
| `tests/test_behavioral.py` | tests for carve-out + tilt | Modify |

`setup_family` STORAGE needs no code: `write_audit_entry` already passes arbitrary payload fields through. Directional entries omit it (treated as `"directional"`); passive entries set `"setup_family": "passive-fade"`.

---

### Task 1: `by_setup_family` breakout in `compute_summary`

**Files:** Modify `audit_log.py` (`compute_summary`); Test `tests/test_audit_log.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_audit_log.py

def _resolved(p, coin, family, r, ts):
    tid = audit_log.write_audit_entry(
        {"coin": coin, "side": "long", "mode": "ENTRY", "verdict": "LONG-NOW",
         "setup_family": family}, ts_ms=ts, path=p)
    audit_log.resolve_audit_entry(tid, r, "T1", path=p, resolved_at_ms=ts + 1000)
    return tid


def test_summary_breaks_out_by_setup_family(tmp_path):
    p = str(tmp_path / "a.jsonl")
    _resolved(p, "HYPE", "passive-fade", 0.5, 1779102720000)
    _resolved(p, "HYPE", "passive-fade", -1.0, 1779102730000)
    _resolved(p, "HYPE", "directional", 2.0, 1779102740000)
    s = audit_log.compute_summary(path=p)
    fam = s["by_setup_family"]
    assert fam["passive-fade"]["n"] == 2
    assert fam["passive-fade"]["win_rate"] == 0.5
    assert fam["passive-fade"]["expectancy_r"] == round((0.5 - 1.0) / 2, 3)
    assert fam["directional"]["n"] == 1
    assert fam["directional"]["expectancy_r"] == 2.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_audit_log.py::test_summary_breaks_out_by_setup_family -v`
Expected: FAIL — `KeyError: 'by_setup_family'`

- [ ] **Step 3: Implement** — in `compute_summary`, after the `by_setup` loop builds `setup_stats`, add a parallel grouping by `setup_family` and include it in the return dict.

Add this block just before the `# "open" mirrors...` comment in `compute_summary`:

```python
    by_family = {}
    for e in resolved:
        fam = e.get("setup_family", "directional")
        by_family.setdefault(fam, []).append(float(e["outcome"]["outcome_r"]))
    family_stats = {}
    for fam, rs in by_family.items():
        n = len(rs)
        family_stats[fam] = {
            "n": n,
            "win_rate": round(sum(1 for r in rs if r > 0) / n, 3) if n else None,
            "expectancy_r": round(sum(rs) / n, 3) if n else None,
            "total_r": round(sum(rs), 3),
        }
```

Then add `"by_setup_family": family_stats,` to the returned dict (next to `"by_setup": setup_stats,`).

- [ ] **Step 4: Run to verify it passes**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_audit_log.py -v`
Expected: PASS (existing + new).

- [ ] **Step 5: Commit** — SKIP (commits held; leave in working tree).

---

### Task 2: `passive_expectancy()` — the PROVEN/UNPROVEN/STOP gate

**Files:** Modify `audit_log.py`; Test `tests/test_audit_log.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_audit_log.py

def test_passive_expectancy_unproven_when_no_samples(tmp_path):
    p = str(tmp_path / "a.jsonl")
    out = audit_log.passive_expectancy(path=p)
    assert out["state"] == "UNPROVEN"
    assert out["n"] == 0
    assert out["size_mult"] == 0.25


def test_passive_expectancy_unproven_below_min_samples(tmp_path):
    p = str(tmp_path / "a.jsonl")
    for i in range(5):
        _resolved(p, "HYPE", "passive-fade", 0.5, 1779102720000 + i * 1000)
    out = audit_log.passive_expectancy(path=p, min_samples=20)
    assert out["state"] == "UNPROVEN"
    assert out["n"] == 5
    assert out["size_mult"] == 0.25


def test_passive_expectancy_proven_when_positive(tmp_path):
    p = str(tmp_path / "a.jsonl")
    # 20 trades, 80% win 0.5R / 20% loss -1.5R -> avg_r = 0.4*0.5... compute:
    # 16 wins * 0.5 = 8.0 ; 4 losses * -1.5 = -6.0 ; sum 2.0 ; avg 0.1 > 0
    for i in range(16):
        _resolved(p, "HYPE", "passive-fade", 0.5, 1779102720000 + i * 1000)
    for i in range(4):
        _resolved(p, "HYPE", "passive-fade", -1.5, 1779102740000 + i * 1000)
    out = audit_log.passive_expectancy(path=p, min_samples=20)
    assert out["state"] == "PROVEN"
    assert out["n"] == 20
    assert out["size_mult"] == 1.0
    assert out["avg_r"] == 0.1


def test_passive_expectancy_stop_when_negative_at_sample_size(tmp_path):
    p = str(tmp_path / "a.jsonl")
    for i in range(10):
        _resolved(p, "HYPE", "passive-fade", 0.5, 1779102720000 + i * 1000)
    for i in range(10):
        _resolved(p, "HYPE", "passive-fade", -1.5, 1779102740000 + i * 1000)
    out = audit_log.passive_expectancy(path=p, min_samples=20)
    assert out["state"] == "STOP"
    assert out["size_mult"] == 0.0
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_audit_log.py -k passive_expectancy -v`
Expected: FAIL — `AttributeError: ... 'passive_expectancy'`

- [ ] **Step 3: Implement** — add to `audit_log.py` (near the summary functions):

```python
# 40, not 20: this strategy needs ~75% win rate just to break even, so 20
# samples is statistically noisy enough to flip PROVEN on luck. 40 is a more
# honest proof bar (costs a longer tiny-size proving period — worth it).
PASSIVE_MIN_SAMPLES = 40
PASSIVE_MIN_SIZE_MULT = 0.25


def passive_expectancy(path=None, min_samples=PASSIVE_MIN_SAMPLES):
    """Audit-derived edge gate for passive-fade trades.

    Returns {state, n, win_rate, avg_r, size_mult}:
      UNPROVEN — fewer than min_samples resolved passive-fade trades; fire at
                 minimum size (size_mult = 0.25) while collecting data.
      PROVEN   — >= min_samples and avg_r > 0; size normally (size_mult = 1.0).
      STOP     — >= min_samples and avg_r <= 0; the setup is net-negative,
                 stop firing it (size_mult = 0.0).
    """
    rs = [float(e["outcome"]["outcome_r"]) for e in _load_entries(path)
          if e.get("setup_family") == "passive-fade" and e.get("outcome")]
    n = len(rs)
    if n == 0:
        return {"state": "UNPROVEN", "n": 0, "win_rate": None,
                "avg_r": None, "size_mult": PASSIVE_MIN_SIZE_MULT}
    win_rate = round(sum(1 for r in rs if r > 0) / n, 3)
    avg_r = round(sum(rs) / n, 3)
    if n < min_samples:
        return {"state": "UNPROVEN", "n": n, "win_rate": win_rate,
                "avg_r": avg_r, "size_mult": PASSIVE_MIN_SIZE_MULT}
    if avg_r > 0:
        return {"state": "PROVEN", "n": n, "win_rate": win_rate,
                "avg_r": avg_r, "size_mult": 1.0}
    return {"state": "STOP", "n": n, "win_rate": win_rate,
            "avg_r": avg_r, "size_mult": 0.0}
```

Also add a CLI subcommand `passive-expectancy` in `main()` that prints `json.dumps(passive_expectancy())` (mirror the existing `summary` branch style), so the skill can read it under `/loop`.

- [ ] **Step 4: Run to verify they pass**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_audit_log.py -v`
Expected: PASS.

- [ ] **Step 5: Commit** — SKIP.

---

### Task 3: behavioral carve-out + passive tilt guard

**Files:** Modify `behavioral.py`; Test `tests/test_behavioral.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_behavioral.py
# (file already does sys.path.insert + import audit_log, behavioral)

def _entry(p, family, leaks, ts, mode="ENTRY"):
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": mode, "verdict": "WAIT",
         "setup_family": family, "behavioral": {"leaks": leaks}},
        ts_ms=ts, path=p)


def test_passive_entries_excluded_from_fomo_streak(tmp_path):
    p = str(tmp_path / "a.jsonl")
    # two leaking PASSIVE entries must NOT build the directional fomo streak
    _entry(p, "passive-fade", ["Timing"], 1779102720000)
    _entry(p, "passive-fade", ["Timing"], 1779102730000)
    st = behavioral.compute_behavioral_state(now_ms=1779102740000, path=p)
    assert st["fomo_streak"] == 0
    assert st["cooldown"]["active"] is False


def test_directional_entries_still_build_fomo_streak(tmp_path):
    p = str(tmp_path / "a.jsonl")
    _entry(p, "directional", ["Timing"], 1779102720000)
    _entry(p, "directional", ["Timing"], 1779102730000)
    st = behavioral.compute_behavioral_state(now_ms=1779102740000, path=p)
    assert st["fomo_streak"] == 2


def test_passive_tilt_active_after_three_consecutive_losses(tmp_path):
    p = str(tmp_path / "a.jsonl")
    base = 1779102720000
    for i in range(3):
        tid = audit_log.write_audit_entry(
            {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "LONG-NOW",
             "setup_family": "passive-fade"}, ts_ms=base + i * 1000, path=p)
        audit_log.resolve_audit_entry(tid, -1.0, "stop", path=p,
                                      resolved_at_ms=base + i * 1000 + 500)
    st = behavioral.compute_behavioral_state(now_ms=base + 10000, path=p)
    assert st["passive_tilt"]["active"] is True
    assert st["passive_tilt"]["loss_streak"] == 3


def test_passive_tilt_resets_on_win(tmp_path):
    p = str(tmp_path / "a.jsonl")
    base = 1779102720000
    rs = [-1.0, -1.0, 0.6]  # most recent (last) is a win -> streak 0
    for i, r in enumerate(rs):
        tid = audit_log.write_audit_entry(
            {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "LONG-NOW",
             "setup_family": "passive-fade"}, ts_ms=base + i * 1000, path=p)
        audit_log.resolve_audit_entry(tid, r, "T1", path=p,
                                      resolved_at_ms=base + i * 1000 + 500)
    st = behavioral.compute_behavioral_state(now_ms=base + 10000, path=p)
    assert st["passive_tilt"]["active"] is False
    assert st["passive_tilt"]["loss_streak"] == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_behavioral.py -k "passive or fomo" -v`
Expected: FAIL (passive entries currently DO build the streak; `passive_tilt` key missing).

- [ ] **Step 3: Implement** in `behavioral.py`:

Add a constant near the others:
```python
PASSIVE_TILT_N = 3
```

In `compute_behavioral_state`, change the entry filter used for FOMO logic to exclude passive-fade. Replace:
```python
    entry_rows = [e for e in entries if e.get("mode") == "ENTRY"]
```
with:
```python
    entry_rows = [e for e in entries
                  if e.get("mode") == "ENTRY"
                  and e.get("setup_family") != "passive-fade"]
```
(Directional FOMO logic now ignores passive fades — they are not directional chases.)

Add a helper near `_has_leaks`:
```python
def _passive_tilt(entries):
    """Trailing consecutive resolved passive-fade LOSSES (range-break signal)."""
    passive_resolved = sorted(
        [e for e in entries
         if e.get("setup_family") == "passive-fade" and e.get("outcome")],
        key=lambda e: int(e["outcome"]["resolved_at_ms"]))
    streak = 0
    for e in reversed(passive_resolved):
        if float(e["outcome"]["outcome_r"]) < 0:
            streak += 1
        else:
            break
    return {"active": streak >= PASSIVE_TILT_N, "loss_streak": streak}
```

Add `"passive_tilt": _passive_tilt(entries),` to the dict returned by `compute_behavioral_state`.

- [ ] **Step 4: Run to verify they pass**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_behavioral.py -v`
Expected: PASS (existing + new).

- [ ] **Step 5: Run full suite**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit** — SKIP.

---

### Phase 2A done criteria
- [ ] `compute_summary` returns `by_setup_family`; `passive_expectancy()` returns the 3-state gate; `behavioral` excludes passive from FOMO and exposes `passive_tilt`.
- [ ] Full suite green.

---

## Phase 2B — Passive-fade module + invocation (DETAILED)

### File Structure (Phase 2B)
| File | Responsibility | Change |
|---|---|---|
| `scalp-passive.md` | New protocol module — gates, both-sides ladder, exit, invalidation, sizing, output | Create |
| `SKILL.md` | Route `/scalp [tiny] passive <COIN>` → load core + passive module | Modify |
| `scalp-core.md` | Note the passive direction-module load + setup_family in the audit payload | Modify |
| `tests/test_skill_consistency.py` | Grep-assert the module documents its required elements | Modify |

No new Python in 2B — edge-proximity is an LLM-computed prose rule (floor/ceiling are judgment from candle structure), and the coarse `fade_ok` + `passive_expectancy` + `passive_tilt` already exist from Plan 1 / Phase 2A. (YAGNI: add a code helper only if proximity proves fuzzy in soak.)

---

### Task B1: Write `scalp-passive.md`

**Files:** Create `scalp-passive.md`; Test deferred to B3.

- [ ] **Step 1: Write the module** — create `scalp-passive.md` with exactly this content:

````markdown
# Scalp — PASSIVE module

Loaded with `scalp-core.md` when the user invokes `/scalp passive <COIN>`.
Provides Steps 2–4 and the passive output deltas. MM-INSPIRED, NOT market
making: a manually-executed, both-sides mean-reversion fade that posts passive
limit ladders at range edges and exits to the mean. Steps 0,1,5,6,M and the
output/journal skeleton come from `scalp-core.md`.

This mode fades BOTH edges of a range; only ONE side arms at a time — whichever
edge price is currently AT.

## Step 2 — Gates (passive; ALL must pass or SIT-OUT / NO-TRADE)

1. **Regime gate (hard):** require `regime.fade_ok == true` AND
   `regime.regime_label == "ranging"`. Otherwise → **SIT-OUT**: print the label
   and the reason — `trending` (directional only; use /scalp long|short),
   `quiet` (no edge either way), `correlated-chop` (just BTC beta — no
   idiosyncratic edge), `unknown` (insufficient data). Passive fading only has
   an edge in genuine two-sided ranging tape.
2. **Expectancy gate (hard):** read `python3 audit_log.py passive-expectancy`.
   If `state == "STOP"` → **NO-TRADE** (the audit log says this mode is
   net-negative over ≥40 trades — stop firing it). Otherwise carry `size_mult`
   into sizing (UNPROVEN → 0.25, PROVEN → 1.0).
3. **Event veto (hard):** binary US econ event today (FOMC/CPI/NFP/PCE) →
   **VETOED / NO-TRADE**, same as the directional modules. (Funding extremes do
   NOT veto a both-sides fade — note crowded funding as context only.)
4. **Tilt (soft):** if `behavioral.passive_tilt.active` (≥3 consecutive passive
   losses) → print `PASSIVE TILT: <n> losses — range likely breaking` and prefer
   SIT-OUT; if proceeding, halve size. Informational, not a hard stop.

## Step 3 — Structure (passive)

From candles: **floor** (≥2 tested lows), **ceiling** (≥2 tested highs),
**mean** (session VWAP; fallback range-mid = (floor+ceiling)/2). The `ranging`
regime label already implies a real range, not a coiling breakout. Define the
**edge zones** = the outer third of the range nearest each boundary. Price
between the inner thirds is mid-range → **WAIT** (no fade arms mid-range).

## Step 4 — Triggers (passive; both-sides, one arms at a time)

- **A Bid-ladder (long fade)** — arms only when ALL hold:
  - price is in the LOWER edge zone (≤ floor + ⅓·range), AND
  - flow into the low is EXHAUSTING (taker_delta sell pressure waning, or
    `buy_share_pct` recovering off the low), AND
  - microprice is NOT leaning hard down through the floor (Step 6d:
    `microprice_dev_bps` not strongly negative).
  Post 2–3 passive limit bids stepped INTO the floor: at floor, floor−X%,
  floor−2X% (X ≈ 0.2–0.4% sized to the range). Never market-take, never chase.
- **B Ask-ladder (short fade)** — symmetric at the ceiling (price in upper edge
  zone; sell-flow exhausting; microprice not leaning hard up).

**Edge-proximity rule:** if price is mid-range (between the inner thirds) →
WAIT, do not arm either ladder. This is the trade-grade `fade_ok` — coarse
regime `fade_ok` PLUS at-an-edge PLUS flow-exhaustion.

Each armed ladder states: entries (the steps) / structural stop (beyond the far
step + buffer; for the bid-ladder below the next pool under the floor) / mean
target / R per step.

## Step 5/6 — Sizing & risk (passive)

- **Sizing:** `risk = equity × cap × size_mult` (cap from `profile.py`;
  `size_mult` from the Step-2 expectancy gate). The WHOLE ladder counts as ONE
  position against `risk` — sum of step sizes ≤ risk (inventory cap; laddering
  never multiplies risk). Show the sizing line per ladder.
- **Session shot-cap:** max 3 passive scalps per session — state the count in
  output; at the cap → WAIT.
- **R:R note:** the directional 2:1 floor does NOT apply — passive is a
  high-win-rate/low-R mode gated by expectancy, not per-trade R:R. (See
  `passive_expectancy`.)

## Exit (passive) — to-the-mean

Target = the mean (session VWAP / range-mid). On fill, post a limit at the mean.
Default = full exit at the mean (small R, high turnover — the MM-inspired core).
Optional: leave a small runner a touch beyond the mean only if flow strongly
supports; default is flat at mean.

## Invalidation (passive) — three exits, FASTEST wins

1. **Hard structural stop** beyond the far ladder step (submitted to HL) — bounds
   the left tail (the range-break loss).
2. **Quick-pull (the MM analog):** if after fill the taker flow turns MORE
   one-sided AGAINST the position (range breaking), exit immediately — do not
   wait for the stop.
3. **Time-stop:** no reversion toward the mean within ~30–60 min → flat.

## Output deltas (passive)

- VERDICT vocab: `FADE-LONG-NOW | FADE-SHORT-NOW | WAIT | SIT-OUT | NO-TRADE | VETOED`.
- Counter-case line is **"Break case"** — one clause on what says the range is
  breaking (which kills the fade).
- Triggers labelled **A (bid-ladder)** / **B (ask-ladder)**.
- Show `EXPECTANCY: <state> (n <n>, win <w>%, avg <avg>R, size ×<mult>)` on the
  verdict line.
- Journal stub `side:` = `long` (bid-ladder) or `short` (ask-ladder);
  `setup_family: passive-fade`.
- TINY: `<COIN> PASSIVE <V> | <expectancy_state> | edge <floor|ceil|mid> @ <mid> | fade_ok <bool> | next <when>`.

## Audit logging (passive)

Log every FADE-*/WAIT/SIT-OUT/NO-TRADE/VETOED with `mode: "ENTRY"`,
`setup_family: "passive-fade"`, and the verdict. JOURNAL STUB shown only for
FADE-LONG-NOW / FADE-SHORT-NOW. On close, resolve with the R-multiple so
`passive_expectancy` learns (UNPROVEN → PROVEN/STOP).
````

- [ ] **Step 2: Sanity-check the file renders** — `cat scalp-passive.md | head -5` and confirm it wrote. (Test coverage is Task B3.)

- [ ] **Step 3: Commit** — SKIP.

---

### Task B2: Wire `/scalp passive` invocation

**Files:** Modify `SKILL.md`, `scalp-core.md`; Test in B3.

- [ ] **Step 1: SKILL.md — direction/module routing.** Find the Step 1 block that decides which direction module to load (the `short` vs default long logic). Add a passive branch BEFORE the long/short split:

```markdown
- Passive: args contain `passive` → load BOTH
  `/Users/nyanyk/Claude/research/scalp/scalp-core.md` and
  `/Users/nyanyk/Claude/research/scalp/scalp-passive.md`. (Overrides long/short:
  passive mode fades both sides.) Invoked as `/scalp passive <COIN>` or
  `/scalp tiny passive <COIN>`.
```

Also, in the Step 3 execution notes where `short`/`tiny`/`deep` args are parsed,
add: ``- `passive` in args = PASSIVE mode (loads scalp-passive.md; both-sides fade).``
And in the audit-logging note: ``- Passive-mode entries set `setup_family: "passive-fade"` in the audit payload (directional entries omit it / default "directional").``

- [ ] **Step 2: scalp-core.md — Step 1 module handoff + Step 0 tilt.** In the Step 1 "hand off to the direction module" sentence, add passive as a third option. In Step 0 (behavioral preflight), add: ``- Passive mode: also read `behavioral.passive_tilt`; if active, print `PASSIVE TILT: <n> losses — informational` and prefer SIT-OUT (Step 2 gate 4 of scalp-passive.md).``

- [ ] **Step 3: Commit** — SKIP.

---

### Task B3: Skill-consistency tests for the passive module

**Files:** Modify `tests/test_skill_consistency.py`

- [ ] **Step 1: Append the failing tests**

```python
def test_passive_module_documents_required_elements():
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "scalp-passive.md")
    with open(path) as f:
        text = f.read()
    # gates
    assert "fade_ok" in text
    assert "passive-expectancy" in text
    assert "STOP" in text and "size_mult" in text
    assert "passive_tilt" in text
    # verdicts + triggers
    for v in ("FADE-LONG-NOW", "FADE-SHORT-NOW", "SIT-OUT"):
        assert v in text
    assert "bid-ladder" in text and "ask-ladder" in text
    # exit + invalidation
    assert "to-the-mean" in text or "to the mean" in text
    assert "quick-pull" in text.lower() or "quick pull" in text.lower()
    assert "time-stop" in text.lower()
    # audit tagging
    assert "passive-fade" in text


def test_skill_routes_passive_invocation():
    import os
    skill = os.path.join(os.path.dirname(__file__), "..", "SKILL.md")
    with open(skill) as f:
        text = f.read()
    assert "scalp-passive.md" in text
    assert "passive" in text.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_skill_consistency.py -v`
Expected: FAIL (module/file not yet referenced) — confirms tests are real.

- [ ] **Step 3: Confirm pass after B1+B2 land**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_skill_consistency.py -v` then full suite `python3 -m pytest -q`.
Expected: PASS. (If a grep assertion fails, fix the WORDING in scalp-passive.md / SKILL.md to match — do not weaken the test.)

- [ ] **Step 4: Commit** — SKIP.

---

### Phase 2B done criteria
- [ ] `scalp-passive.md` exists and documents gates, both-sides ladder, to-the-mean exit, 3-way invalidation, expectancy sizing, output + audit tagging.
- [ ] `/scalp passive <COIN>` routes to the module; `setup_family: passive-fade` flows into the audit log.
- [ ] Skill-consistency tests pass; full suite green.

## Phase 2C — Replay simulation for passive exits (OUTLINE; detail after 2B)

Files (≤3): `replay.py`, `tests/test_replay.py`.

Tasks:
1. In `replay.py`, branch on `setup_family == "passive-fade"`: simulate ladder fill (touch of a ladder step), to-the-mean exit (target = mean), time-stop (flat after N bars), hard stop. Score R against the structural stop distance. Mirror long/short via the existing negation trick.
2. Tests for: filled→mean (win), filled→stop (loss), filled→time-stop (scratch), never-filled (unfilled).

---

## After Plan 2
- Soak passive mode live at min size (UNPROVEN). The audit log accumulates resolved passive-fade trades; `passive_expectancy` flips to PROVEN (size up) or STOP (kill) on real data — the design's honest edge test.
- If PROVEN holds, consider promoting the regime classifier to auto-routing (the original Approach 2) so every `/scalp` call routes momentum/fade/sit-out automatically.
