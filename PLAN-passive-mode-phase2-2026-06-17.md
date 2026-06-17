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

## Phase 2B — Passive-fade module + invocation (OUTLINE; detail after 2A)

Authored in full once 2A is committed. Files (≤5): `scalp-passive.md` (new), `scalp-core.md`, `SKILL.md`, `tests/test_skill_consistency.py`, plus an optional `regime.py` helper for edge-proximity.

Tasks:
1. **`scalp-passive.md`** (new protocol module, prose like `scalp-long.md`):
   - **Gate:** require `regime.fade_ok == true` (regime_label == ranging). Else print the regime label + "sit out — directional/quiet/correlated tape" and STOP.
   - **Both-sides ladder:** identify range floor/ceiling/mean (session VWAP, fallback range-mid). Post a passive BID ladder (2–3 steps) into the floor and a passive ASK ladder into the ceiling. Microprice gate (reuse Step 6d) disarms a ladder leaning through its edge.
   - **Trade-grade edge-proximity `fade_ok`:** the coarse Phase-1 `fade_ok` plus the LLM-computed check that price is AT an edge (not mid-range) and flow into the edge is exhausting (taker_delta absorption).
   - **Exit:** to-the-mean (post limit at mean). Small R by design.
   - **Invalidation:** hard structural stop beyond the ladder; quick-pull on adverse flow flip; time-stop ~30–60 min.
   - **Sizing:** read `audit_log.py passive-expectancy`; multiply the profile risk cap by `size_mult` (UNPROVEN → 0.25, PROVEN → 1.0, STOP → do not trade). Whole ladder counts as one position vs the cap (inventory cap). Session shot-cap.
   - **Output:** compact passive block + `PROVEN|UNPROVEN|STOP` status; TINY one-liner.
2. **`scalp-core.md` + `SKILL.md`:** wire `/scalp passive <COIN>` and `/scalp tiny passive <COIN>`; Step 0 surfaces `passive_tilt` (informational cooldown on fading); audit payload carries `setup_family: "passive-fade"`.
3. **`tests/test_skill_consistency.py`:** assert the module documents the fade_ok gate, expectancy sizing, three-way invalidation, and the passive invocation.

## Phase 2C — Replay simulation for passive exits (OUTLINE; detail after 2B)

Files (≤3): `replay.py`, `tests/test_replay.py`.

Tasks:
1. In `replay.py`, branch on `setup_family == "passive-fade"`: simulate ladder fill (touch of a ladder step), to-the-mean exit (target = mean), time-stop (flat after N bars), hard stop. Score R against the structural stop distance. Mirror long/short via the existing negation trick.
2. Tests for: filled→mean (win), filled→stop (loss), filled→time-stop (scratch), never-filled (unfilled).

---

## After Plan 2
- Soak passive mode live at min size (UNPROVEN). The audit log accumulates resolved passive-fade trades; `passive_expectancy` flips to PROVEN (size up) or STOP (kill) on real data — the design's honest edge test.
- If PROVEN holds, consider promoting the regime classifier to auto-routing (the original Approach 2) so every `/scalp` call routes momentum/fade/sit-out automatically.
