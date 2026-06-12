import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import audit_log
import behavioral

HOUR = 3_600_000
DAY = 24 * HOUR
NOW = 1_780_000_000_000


def _p(tmp_path):
    return str(tmp_path / "a.jsonl")


def _entry(p, ts, verdict="WAIT", leaks=None, mode="ENTRY", plan_status=None):
    payload = {"coin": "HYPE", "side": "long", "mode": mode, "verdict": verdict,
               "behavioral": {"leaks": leaks or []}}
    if plan_status:
        payload["plan_status"] = plan_status
    return audit_log.write_audit_entry(payload, ts_ms=ts, path=p)


def test_empty_log_all_clear(tmp_path):
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=_p(tmp_path))
    assert st["cooldown"]["active"] is False
    assert st["fomo_streak"] == 0
    assert st["oop_this_week"] == 0


def test_loss_cooldown_active_within_24h(tmp_path):
    p = _p(tmp_path)
    tid = _entry(p, NOW - 2 * HOUR)
    audit_log.resolve_audit_entry(tid, -1.0, "stop",
                                  resolved_at_ms=NOW - HOUR, path=p)
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["cooldown"]["active"] is True
    assert st["cooldown"]["until_ms"] == NOW - HOUR + DAY
    assert any("lost" in r for r in st["cooldown"]["reasons"])


def test_loss_cooldown_expired_after_24h(tmp_path):
    p = _p(tmp_path)
    tid = _entry(p, NOW - 40 * HOUR)
    audit_log.resolve_audit_entry(tid, -1.0, "stop",
                                  resolved_at_ms=NOW - 30 * HOUR, path=p)
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["cooldown"]["active"] is False


def test_small_loss_below_threshold_no_cooldown(tmp_path):
    p = _p(tmp_path)
    tid = _entry(p, NOW - 2 * HOUR)
    audit_log.resolve_audit_entry(tid, -0.5, "stop",
                                  resolved_at_ms=NOW - HOUR, path=p)
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["cooldown"]["active"] is False


def test_win_no_cooldown(tmp_path):
    p = _p(tmp_path)
    tid = _entry(p, NOW - 2 * HOUR)
    audit_log.resolve_audit_entry(tid, 2.0, "T2",
                                  resolved_at_ms=NOW - HOUR, path=p)
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["cooldown"]["active"] is False


def test_fomo_cooldown_last_two_entries_leak(tmp_path):
    p = _p(tmp_path)
    _entry(p, NOW - 5 * HOUR, leaks=["timing"])
    _entry(p, NOW - 2 * HOUR, leaks=["timing", "state"])
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["cooldown"]["active"] is True
    assert st["cooldown"]["until_ms"] == NOW - 2 * HOUR + 3 * DAY
    assert any("FOMO" in r for r in st["cooldown"]["reasons"])


def test_fomo_needs_two_consecutive(tmp_path):
    p = _p(tmp_path)
    _entry(p, NOW - 5 * HOUR, leaks=[])          # clean
    _entry(p, NOW - 2 * HOUR, leaks=["timing"])  # only latest leaks
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["cooldown"]["active"] is False
    assert st["fomo_streak"] == 1


def test_fomo_streak_counts_trailing_only(tmp_path):
    p = _p(tmp_path)
    _entry(p, NOW - 9 * HOUR, leaks=["timing"])
    _entry(p, NOW - 6 * HOUR, leaks=[])          # breaks the streak
    _entry(p, NOW - 3 * HOUR, leaks=["state"])
    _entry(p, NOW - 1 * HOUR, leaks=["timing"])
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["fomo_streak"] == 2


def test_manage_entries_ignored_for_fomo(tmp_path):
    p = _p(tmp_path)
    _entry(p, NOW - 5 * HOUR, leaks=["timing"])
    _entry(p, NOW - 2 * HOUR, leaks=[], mode="MANAGE")  # not an ENTRY
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["fomo_streak"] == 1


def test_oop_this_week_count(tmp_path):
    p = _p(tmp_path)
    _entry(p, NOW - 1 * HOUR, plan_status="OOP-1")    # this week
    _entry(p, NOW - 10 * DAY, plan_status="OOP-1")    # last week+
    _entry(p, NOW - 2 * HOUR, plan_status="in-plan")  # not OOP
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["oop_this_week"] == 1


def test_combined_cooldown_takes_latest_window(tmp_path):
    p = _p(tmp_path)
    # loss cooldown ends sooner; fomo cooldown ends later -> report fomo end
    tid = _entry(p, NOW - 5 * HOUR, leaks=["timing"])
    audit_log.resolve_audit_entry(tid, -1.0, "stop",
                                  resolved_at_ms=NOW - 4 * HOUR, path=p)
    _entry(p, NOW - 2 * HOUR, leaks=["state"])
    st = behavioral.compute_behavioral_state(now_ms=NOW, path=p)
    assert st["cooldown"]["active"] is True
    assert st["cooldown"]["until_ms"] == NOW - 2 * HOUR + 3 * DAY
    assert len(st["cooldown"]["reasons"]) == 2
