import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import audit_log
import replay


def c(t, o, h, l, cl):
    return {"t": t, "o": o, "h": h, "l": l, "c": cl}


TRIG = {"entry": 100.0, "stop": 98.0, "t1": 104.0, "t2": 106.0}
# risk 2.0 -> r_t1 = 2.0, r_t2 = 3.0
T0 = 1_780_000_000_000
M5 = 300_000


def test_r_levels():
    lv = replay.trigger_r_levels(TRIG, "long")
    assert lv["risk"] == 2.0
    assert lv["r_t1"] == 2.0
    assert lv["r_t2"] == 3.0
    sh = replay.trigger_r_levels(
        {"entry": 100.0, "stop": 102.0, "t1": 96.0, "t2": 94.0}, "short")
    assert sh["risk"] == 2.0
    assert sh["r_t1"] == 2.0
    assert sh["r_t2"] == 3.0


def test_r_levels_no_t2():
    lv = replay.trigger_r_levels({"entry": 100.0, "stop": 98.0, "t1": 104.0,
                                  "t2": None}, "long")
    assert lv["r_t2"] is None


def test_unfilled():
    candles = [c(T0, 101, 102, 100.5, 101.5), c(T0 + M5, 101.5, 103, 101, 102)]
    out = replay.simulate_trigger(TRIG, "long", candles)
    assert out["status"] == "unfilled"
    assert out["r"] is None
    assert out["ambiguous"] is False


def test_fill_then_stop():
    candles = [
        c(T0, 101, 101.5, 99.9, 100.2),       # touches entry, not stop
        c(T0 + M5, 100.2, 100.8, 97.9, 98.1), # hits stop
    ]
    out = replay.simulate_trigger(TRIG, "long", candles)
    assert out["status"] == "stopped"
    assert out["r"] == -1.0
    assert out["ambiguous"] is False


def test_fill_candle_also_hits_stop_is_deterministic_stop():
    # resting order at entry fills on the way down to the stop — order knowable
    candles = [c(T0, 101, 101.5, 97.8, 98.5)]
    out = replay.simulate_trigger(TRIG, "long", candles)
    assert out["status"] == "stopped"
    assert out["r"] == -1.0
    assert out["ambiguous"] is False


def test_fill_then_t1_then_t2():
    candles = [
        c(T0, 101, 101.5, 99.9, 100.5),
        c(T0 + M5, 100.5, 104.2, 100.4, 104.0),    # T1: bank 0.5 @ 2R, BE armed
        c(T0 + 2 * M5, 104.0, 106.3, 103.9, 106.1) # T2 without touching entry
    ]
    out = replay.simulate_trigger(TRIG, "long", candles)
    assert out["status"] == "t1_t2"
    assert out["r"] == pytest.approx(0.5 * 2.0 + 0.5 * 3.0)
    assert out["ambiguous"] is False


def test_fill_then_t1_then_breakeven():
    candles = [
        c(T0, 101, 101.5, 99.9, 100.5),
        c(T0 + M5, 100.5, 104.2, 100.4, 104.0),     # T1 banked, stop -> entry
        c(T0 + 2 * M5, 104.0, 104.5, 99.8, 100.1),  # dips through entry -> BE out
    ]
    out = replay.simulate_trigger(TRIG, "long", candles)
    assert out["status"] == "t1_be"
    assert out["r"] == pytest.approx(0.5 * 2.0)
    assert out["ambiguous"] is False


def test_t1_with_no_t2_defined_ends_at_t1_be_convention():
    trig = {"entry": 100.0, "stop": 98.0, "t1": 104.0, "t2": None}
    candles = [
        c(T0, 101, 101.5, 99.9, 100.5),
        c(T0 + M5, 100.5, 104.2, 100.4, 104.0),
    ]
    out = replay.simulate_trigger(trig, "long", candles)
    assert out["status"] == "t1_be"
    assert out["r"] == pytest.approx(0.5 * 2.0)


def test_same_candle_stop_and_t1_conservative_without_drill():
    candles = [
        c(T0, 101, 101.5, 99.9, 100.5),
        c(T0 + M5, 100.5, 104.5, 97.5, 100.0),  # both stop and t1 in range
    ]
    out = replay.simulate_trigger(TRIG, "long", candles)
    assert out["status"] == "stopped"
    assert out["r"] == -1.0
    assert out["ambiguous"] is True


def test_same_candle_stop_and_t1_drill_resolves():
    base = [
        c(T0, 101, 101.5, 99.9, 100.5),
        c(T0 + M5, 100.5, 104.5, 97.5, 100.0),
    ]

    def fetch_finer(start_ms, end_ms):
        assert start_ms == T0 + M5
        return [
            c(start_ms, 100.5, 104.2, 100.3, 104.0),         # t1 first
            c(start_ms + 60_000, 104.0, 104.5, 97.5, 100.0), # then to stop -> BE out
        ]

    out = replay.simulate_trigger(TRIG, "long", base, fetch_finer=fetch_finer)
    assert out["status"] == "t1_be"
    assert out["r"] == pytest.approx(0.5 * 2.0)
    assert out["ambiguous"] is False


def test_fill_candle_touching_t1_is_not_banked_without_drill():
    # entry + t1 in the fill candle, no stop: order unknowable -> T1 not banked,
    # position continues as plain FILLED, flagged ambiguous
    candles = [
        c(T0, 105, 105.5, 99.9, 104.5),               # touches entry AND t1
        c(T0 + 2 * M5, 104.5, 104.6, 97.9, 98.0),     # then stops out
    ]
    out = replay.simulate_trigger(TRIG, "long", candles)
    assert out["status"] == "stopped"
    assert out["r"] == -1.0
    assert out["ambiguous"] is True


def test_expired_mark_to_market():
    candles = [
        c(T0, 101, 101.5, 99.9, 100.5),
        c(T0 + M5, 100.5, 102.0, 100.0, 101.0),  # window ends, no stop/t1
    ]
    out = replay.simulate_trigger(TRIG, "long", candles)
    assert out["status"] == "expired"
    assert out["r"] == pytest.approx((101.0 - 100.0) / 2.0)


def test_short_mirror_stop_and_ladder():
    trig = {"entry": 100.0, "stop": 102.0, "t1": 96.0, "t2": 94.0}
    stopped = [
        c(T0, 99, 100.2, 98.8, 99.5),               # fills (touch entry)
        c(T0 + M5, 99.5, 102.2, 99.4, 102.0),       # stop above hit
    ]
    out = replay.simulate_trigger(trig, "short", stopped)
    assert out["status"] == "stopped"
    assert out["r"] == -1.0

    ladder = [
        c(T0, 99, 100.2, 98.8, 99.5),
        c(T0 + M5, 99.5, 99.6, 95.8, 96.0),          # T1 down
        c(T0 + 2 * M5, 96.0, 96.5, 93.8, 94.0),      # T2 down, entry untouched
    ]
    out = replay.simulate_trigger(trig, "short", ladder)
    assert out["status"] == "t1_t2"
    assert out["r"] == pytest.approx(0.5 * 2.0 + 0.5 * 3.0)


# ---------- apply_replay_to_entry ----------

def _entry(verdict="WAIT", triggers=None, outcome=None, mode="ENTRY"):
    return {"id": "scalp-x", "ts_ms": T0, "coin": "HYPE", "side": "long",
            "mode": mode, "verdict": verdict,
            "triggers": triggers, "outcome": outcome}


def test_apply_replay_scores_each_trigger_and_best_r():
    e = _entry(triggers={"A": dict(TRIG), "B": None})
    candles = [
        c(T0, 101, 101.5, 99.9, 100.5),
        c(T0 + M5, 100.5, 104.2, 100.4, 104.0),
        c(T0 + 2 * M5, 104.0, 106.3, 103.9, 106.1),
    ]
    cf = replay.apply_replay_to_entry(e, candles, window_h=72)
    assert cf["per_trigger"]["A"]["status"] == "t1_t2"
    assert "B" not in cf["per_trigger"]
    assert cf["best_r"] == pytest.approx(2.5)
    assert cf["window_h"] == 72


def test_apply_replay_skips_resolved_and_manage_and_empty():
    resolved = _entry(outcome={"outcome_r": 1.0})
    assert replay.apply_replay_to_entry(resolved, [], window_h=72) is None
    manage = _entry(mode="MANAGE")
    assert replay.apply_replay_to_entry(manage, [], window_h=72) is None
    no_trig = _entry(triggers={"A": None, "B": None})
    assert replay.apply_replay_to_entry(no_trig, [], window_h=72) is None


def test_apply_replay_unfilled_best_r_is_none():
    e = _entry(triggers={"A": dict(TRIG)})
    candles = [c(T0, 101, 102, 100.5, 101.5)]
    cf = replay.apply_replay_to_entry(e, candles, window_h=72)
    assert cf["per_trigger"]["A"]["status"] == "unfilled"
    assert cf["best_r"] is None


# ---------- audit_log integration ----------

def _write(p, payload, ts):
    return audit_log.write_audit_entry(payload, ts_ms=ts, path=p)


def test_summary_counterfactual_section(tmp_path):
    p = str(tmp_path / "a.jsonl")
    base = {"coin": "HYPE", "side": "long", "mode": "ENTRY"}
    t1 = _write(p, {**base, "verdict": "WAIT"}, T0)
    t2 = _write(p, {**base, "verdict": "VETOED"}, T0 + 1)
    t3 = _write(p, {**base, "verdict": "WAIT"}, T0 + 2)

    audit_log.attach_counterfactual(t1, {
        "window_h": 72, "best_r": 2.5,
        "per_trigger": {"A": {"status": "t1_t2", "r": 2.5, "ambiguous": False}},
    }, path=p)
    audit_log.attach_counterfactual(t2, {
        "window_h": 72, "best_r": -1.0,
        "per_trigger": {"A": {"status": "stopped", "r": -1.0, "ambiguous": False}},
    }, path=p)
    # t3 left unscored

    s = audit_log.compute_summary(path=p)
    cf = s["counterfactual"]
    assert cf["scored"] == 2
    assert cf["wait"]["n"] == 1
    assert cf["wait"]["fired"] == 1
    assert cf["wait"]["missed_r"] == pytest.approx(2.5)
    assert cf["veto"]["n"] == 1
    assert cf["veto"]["avoided_r"] == pytest.approx(-1.0)
    assert cf["by_setup"]["long-A"]["n"] == 2
    assert cf["by_setup"]["long-A"]["expectancy_r"] == pytest.approx(0.75)


def test_list_open_excludes_counterfactually_resolved_nonaction(tmp_path):
    p = str(tmp_path / "a.jsonl")
    base = {"coin": "HYPE", "side": "long", "mode": "ENTRY"}
    wait_scored = _write(p, {**base, "verdict": "WAIT"}, T0)
    wait_raw = _write(p, {**base, "verdict": "WAIT"}, T0 + 1)
    action = _write(p, {**base, "verdict": "LONG-NOW", "trigger_used": "A"}, T0 + 2)

    audit_log.attach_counterfactual(wait_scored, {
        "window_h": 72, "best_r": None, "per_trigger": {}}, path=p)
    # action verdict also gets a counterfactual — must STAY open (real trade
    # needs a real resolve)
    audit_log.attach_counterfactual(action, {
        "window_h": 72, "best_r": 1.0,
        "per_trigger": {"A": {"status": "t1_be", "r": 1.0, "ambiguous": False}},
    }, path=p)

    open_ids = {e["id"] for e in audit_log.list_open_entries(path=p)}
    assert wait_scored not in open_ids
    assert wait_raw in open_ids
    assert action in open_ids


def test_attach_counterfactual_immutable_unless_force(tmp_path):
    p = str(tmp_path / "a.jsonl")
    tid = _write(p, {"coin": "HYPE", "side": "long", "mode": "ENTRY",
                     "verdict": "WAIT"}, T0)
    assert audit_log.attach_counterfactual(tid, {"window_h": 72, "best_r": None,
                                                 "per_trigger": {}}, path=p)
    assert not audit_log.attach_counterfactual(tid, {"window_h": 48,
                                                     "best_r": 1.0,
                                                     "per_trigger": {}}, path=p)
    assert audit_log.attach_counterfactual(tid, {"window_h": 48, "best_r": 1.0,
                                                 "per_trigger": {}},
                                           path=p, force=True)
    e = [x for x in audit_log._load_entries(p) if x["id"] == tid][0]
    assert e["counterfactual"]["window_h"] == 48


def test_apply_replay_skips_malformed_triggers_keeps_scoreable():
    # real-world schema drift: some triggers are stubs like {'status': 'armed'}
    # or zones without stop/targets — they must be skipped, not crash
    e = _entry(triggers={
        "A": {"entry_zone": "100-101", "status": "watching", "type": "sweep"},
        "B": dict(TRIG),
        "C": {"status": "invalidated"},
    })
    candles = [
        c(T0, 101, 101.5, 99.9, 100.5),
        c(T0 + M5, 100.5, 104.2, 100.4, 104.0),
        c(T0 + 2 * M5, 104.0, 106.3, 103.9, 106.1),
    ]
    cf = replay.apply_replay_to_entry(e, candles, window_h=72)
    assert list(cf["per_trigger"].keys()) == ["B"]
    assert cf["per_trigger"]["B"]["status"] == "t1_t2"
    assert cf["unscoreable"] == ["A", "C"]


def test_apply_replay_all_malformed_returns_none():
    e = _entry(triggers={"A": {"status": "armed"}, "B": {"entry": 100.0}})
    assert replay.apply_replay_to_entry(e, [], window_h=72) is None


def test_replay_open_entries_closes_out_unscoreable_nonaction(tmp_path, monkeypatch):
    # entries whose triggers are all stubs can never be scored — they must be
    # closed out (counterfactual attached with empty per_trigger) so list-open
    # stops reporting them forever
    p = str(tmp_path / "a.jsonl")
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    tid = _write(p, {"coin": "HYPE", "side": "long", "mode": "ENTRY",
                     "verdict": "WAIT",
                     "triggers": {"A": {"status": "armed"}}}, T0)
    monkeypatch.setattr(replay, "fetch_candles_ms",
                        lambda *a, **k: [c(T0, 101, 102, 100.5, 101.5)])
    out = replay.replay_open_entries(window_h=72)
    assert any(s["id"] == tid and "unscoreable" in s["reason"]
               for s in out["skipped"])
    assert audit_log.list_open_entries(path=p) == []


def test_summary_open_count_matches_list_open_semantics(tmp_path):
    p = str(tmp_path / "a.jsonl")
    base = {"coin": "HYPE", "side": "long", "mode": "ENTRY"}
    scored = _write(p, {**base, "verdict": "WAIT"}, T0)
    _write(p, {**base, "verdict": "WAIT"}, T0 + 1)          # raw -> open
    _write(p, {**base, "verdict": "LONG-NOW"}, T0 + 2)      # action -> open
    audit_log.attach_counterfactual(scored, {
        "window_h": 72, "best_r": None, "per_trigger": {}}, path=p)
    s = audit_log.compute_summary(path=p)
    assert s["open"] == 2


def test_passive_fade_entries_are_skipped_by_replay():
    # A passive-fade action entry, even with a directional-looking trigger that
    # WOULD otherwise score (entry filled + t1 hit), must be skipped — passive
    # trades resolve manually, not via the directional ladder sim.
    entry = {
        "id": "x", "ts_ms": 1, "mode": "ENTRY", "side": "long",
        "verdict": "FADE-LONG-NOW", "setup_family": "passive-fade",
        "outcome": None,
        "triggers": {"A": {"entry": 100.0, "stop": 99.0, "t1": 101.0}},
    }
    candles = [c(1, 100.0, 101.0, 99.5, 101.0)]  # would score +1R if not skipped
    assert replay.apply_replay_to_entry(entry, candles, 72) is None


def test_directional_entry_still_scored_after_passive_guard():
    # Regression: the passive guard must not affect directional entries.
    entry = {
        "id": "y", "ts_ms": 1, "mode": "ENTRY", "side": "long",
        "verdict": "LONG-NOW", "outcome": None,
        "triggers": {"A": {"entry": 100.0, "stop": 99.0, "t1": 101.0}},
    }
    candles = [c(1, 100.0, 101.0, 99.5, 101.0)]
    cf = replay.apply_replay_to_entry(entry, candles, 72)
    assert cf is not None
    assert "per_trigger" in cf and "A" in cf["per_trigger"]
