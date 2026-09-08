import audit_log


def test_management_paper_and_unfilled_rows_do_not_become_entry_alpha(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    base = {"coin": "HYPE", "side": "long", "mode": "ENTRY", "verdict": "LONG-NOW", "trigger_used": "A"}
    for index, change in enumerate([{}, {"mode": "MANAGE"}, {"paper": True},
                                     {"filled": False}, {"verdict": "WAIT"}]):
        tid = audit_log.write_audit_entry({**base, **change}, ts_ms=1000 + index, path=path)
        audit_log.resolve_audit_entry(tid, 1.0, "test", path=path)
    summary = audit_log.compute_summary(path=path)
    assert summary["resolved_rows"] == 5
    assert summary["resolved"] == 1
    assert summary["by_setup"]["legacy/directional/long/A"]["n"] == 1


def test_identical_setup_labels_do_not_pool_distinct_systems(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    for index, system in enumerate(["first", "second"]):
        tid = audit_log.write_audit_entry({"coin": "HYPE", "side": "short", "mode": "ENTRY",
              "verdict": "SHORT-NOW", "trigger_used": "A", "system": system}, ts_ms=1000 + index, path=path)
        audit_log.resolve_audit_entry(tid, index * 2 - 1, "test", path=path)
    summary = audit_log.compute_summary(path=path)
    assert len(summary["by_setup"]) == 2
    assert summary["by_setup"]["first/directional/short/A"]["expectancy_r"] == -1
