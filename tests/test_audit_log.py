import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import audit_log


def _path(tmp_path):
    return str(tmp_path / "audit.jsonl")


def test_write_audit_entry_generates_id_and_appends(tmp_path):
    p = _path(tmp_path)
    tid = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY",
         "verdict": "WAIT", "conviction": "med"},
        ts_ms=1779102720000, path=p)
    assert tid.startswith("scalp-")
    assert "HYPE" in tid
    assert "long" in tid
    entries = audit_log._load_entries(p)
    assert len(entries) == 1
    assert entries[0]["verdict"] == "WAIT"
    assert entries[0]["outcome"] is None
    assert entries[0]["id"] == tid
    # ts fields are present and consistent
    assert entries[0]["ts_ms"] == 1779102720000
    assert "ts_utc" in entries[0]


def test_write_audit_entry_collision_appends_suffix(tmp_path):
    p = _path(tmp_path)
    ts = 1779102720000
    tid1 = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT"},
        ts_ms=ts, path=p)
    tid2 = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT"},
        ts_ms=ts, path=p)
    tid3 = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT"},
        ts_ms=ts, path=p)
    assert tid1 != tid2 != tid3
    assert tid2.endswith("-2")
    assert tid3.endswith("-3")


def test_write_audit_entry_requires_core_fields(tmp_path):
    p = _path(tmp_path)
    with pytest.raises(ValueError, match="side"):
        audit_log.write_audit_entry({"coin": "HYPE"}, path=p)
    with pytest.raises(ValueError, match="verdict"):
        audit_log.write_audit_entry(
            {"coin": "HYPE", "side": "long", "mode": "ENTRY"}, path=p)


def test_write_audit_entry_strips_caller_supplied_owned_fields(tmp_path):
    """id, ts_ms, ts_utc, outcome are owned by this module — callers can't override."""
    p = _path(tmp_path)
    tid = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT",
         "id": "fake-id", "ts_ms": 1, "outcome": {"r": 999}},
        ts_ms=1779102720000, path=p)
    assert tid != "fake-id"
    entries = audit_log._load_entries(p)
    assert entries[0]["id"] == tid
    assert entries[0]["ts_ms"] == 1779102720000
    assert entries[0]["outcome"] is None


def test_resolve_audit_entry_fills_outcome(tmp_path):
    p = _path(tmp_path)
    tid = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "LONG-NOW",
         "trigger_used": "A"},
        ts_ms=1779102720000, path=p)
    ok = audit_log.resolve_audit_entry(
        tid, -1.0, "stop", "BTC dump took out the floor",
        resolved_at_ms=1779200000000, path=p)
    assert ok is True
    entries = audit_log._load_entries(p)
    assert entries[0]["outcome"]["outcome_r"] == -1.0
    assert entries[0]["outcome"]["exit_reason"] == "stop"
    assert entries[0]["outcome"]["lesson"] == "BTC dump took out the floor"
    assert entries[0]["outcome"]["resolved_at_ms"] == 1779200000000
    assert "resolved_at_utc" in entries[0]["outcome"]


def test_resolve_audit_entry_unknown_id_returns_false(tmp_path):
    p = _path(tmp_path)
    assert audit_log.resolve_audit_entry("nope", 1.0, "T1", path=p) is False


def test_resolve_audit_entry_immutable_once_resolved(tmp_path):
    p = _path(tmp_path)
    tid = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "LONG-NOW"},
        path=p)
    audit_log.resolve_audit_entry(tid, 1.0, "T1", path=p)
    # second resolve attempt is a no-op
    assert audit_log.resolve_audit_entry(tid, 99.0, "T2", path=p) is False
    entries = audit_log._load_entries(p)
    assert entries[0]["outcome"]["outcome_r"] == 1.0
    assert entries[0]["outcome"]["exit_reason"] == "T1"


def test_list_open_entries_returns_unresolved_with_age(tmp_path):
    p = _path(tmp_path)
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "LONG-NOW"},
        ts_ms=1779000000000, path=p)
    tid2 = audit_log.write_audit_entry(
        {"coin": "BTC", "side": "short", "mode": "ENTRY", "verdict": "SHORT-NOW"},
        ts_ms=1779100000000, path=p)
    audit_log.resolve_audit_entry(tid2, 2.0, "T2", path=p)
    open_entries = audit_log.list_open_entries(now_ms=1779200000000, path=p)
    assert len(open_entries) == 1
    assert open_entries[0]["coin"] == "HYPE"
    # 1779200000000 - 1779000000000 = 200_000_000 ms = ~55.5h
    assert open_entries[0]["age_h"] == round(200_000_000 / 3_600_000, 1)


def test_compute_summary_aggregates_verdict_and_setup(tmp_path):
    p = _path(tmp_path)
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT"},
        path=p)
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT"},
        path=p)
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "VETOED"},
        path=p)
    tid_win = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY",
         "verdict": "LONG-NOW", "trigger_used": "A"},
        path=p)
    tid_loss = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY",
         "verdict": "LONG-NOW", "trigger_used": "B"},
        path=p)
    audit_log.resolve_audit_entry(tid_win, 2.5, "T2", path=p)
    audit_log.resolve_audit_entry(tid_loss, -1.0, "stop", path=p)

    summary = audit_log.compute_summary(path=p)
    assert summary["total_entries"] == 5
    assert summary["resolved"] == 2
    assert summary["open"] == 3
    assert summary["verdict_distribution"]["WAIT"] == 2
    assert summary["verdict_distribution"]["VETOED"] == 1
    assert summary["verdict_distribution"]["LONG-NOW"] == 2
    assert summary["by_setup"]["A"]["n"] == 1
    assert summary["by_setup"]["A"]["expectancy_r"] == 2.5
    assert summary["by_setup"]["A"]["win_rate"] == 1.0
    assert summary["by_setup"]["B"]["expectancy_r"] == -1.0
    assert summary["by_setup"]["B"]["win_rate"] == 0.0


def test_compute_summary_since_days_filters_old_entries(tmp_path):
    p = _path(tmp_path)
    now = 1779_000_000_000
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT"},
        ts_ms=now - 40 * 86_400_000, path=p)
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT"},
        ts_ms=now, path=p)
    summary = audit_log.compute_summary(since_days=30, path=p, now_ms=now)
    assert summary["total_entries"] == 1


def test_compute_summary_empty_log(tmp_path):
    p = _path(tmp_path)
    summary = audit_log.compute_summary(path=p)
    assert summary["total_entries"] == 0
    assert summary["resolved"] == 0
    assert summary["open"] == 0
    assert summary["verdict_distribution"] == {}
    assert summary["by_setup"] == {}


def test_load_entries_skips_malformed_lines(tmp_path):
    p = _path(tmp_path)
    # Mix valid and malformed lines
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT"},
        ts_ms=1779_000_000_000, path=p)
    with open(p, "a") as f:
        f.write("not-json-at-all\n")
        f.write("\n")  # blank line
        f.write('{"valid": "but-incomplete"}\n')  # parses but missing fields
    entries = audit_log._load_entries(p)
    # Original + the "valid but incomplete" — both load (no schema validation on read)
    assert len(entries) == 2


def test_cli_log_via_stdin(tmp_path, monkeypatch, capsys):
    p = _path(tmp_path)
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    payload = json.dumps({
        "coin": "HYPE", "side": "long", "mode": "ENTRY",
        "verdict": "LONG-NOW", "trigger_used": "A",
    })
    monkeypatch.setattr("sys.stdin", _StringIO(payload))
    rc = audit_log.main(["audit_log.py", "log"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("scalp-")


def test_cli_log_rejects_invalid_json(tmp_path, monkeypatch, capsys):
    p = _path(tmp_path)
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    monkeypatch.setattr("sys.stdin", _StringIO("not json"))
    rc = audit_log.main(["audit_log.py", "log"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid JSON" in err


def test_cli_resolve_happy_path(tmp_path, monkeypatch, capsys):
    p = _path(tmp_path)
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    tid = audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "LONG-NOW"},
        path=p)
    rc = audit_log.main(
        ["audit_log.py", "resolve", tid, "1.5", "T1", "clean entry, T1 hit"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"resolved {tid}" in out
    entries = audit_log._load_entries(p)
    assert entries[0]["outcome"]["outcome_r"] == 1.5
    assert entries[0]["outcome"]["lesson"] == "clean entry, T1 hit"


def test_cli_resolve_unknown_id_returns_1(tmp_path, monkeypatch, capsys):
    p = _path(tmp_path)
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    rc = audit_log.main(["audit_log.py", "resolve", "nope", "1.0", "T1"])
    assert rc == 1


def test_cli_summary_outputs_json(tmp_path, monkeypatch, capsys):
    p = _path(tmp_path)
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "WAIT"},
        path=p)
    rc = audit_log.main(["audit_log.py", "summary"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["total_entries"] == 1
    assert parsed["verdict_distribution"]["WAIT"] == 1


def test_cli_unknown_command(monkeypatch, capsys):
    rc = audit_log.main(["audit_log.py", "wat"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown command" in err


# Tiny stand-in for io.StringIO that's friendlier under monkeypatch
class _StringIO:
    def __init__(self, s):
        self._s = s
    def read(self):
        return self._s
