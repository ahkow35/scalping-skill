import audit_log


def test_summary_system_filter(tmp_path):
    path = str(tmp_path / "log.json")
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY",
         "verdict": "CARD", "system": "scan2"}, path=path)
    audit_log.write_audit_entry(
        {"coin": "HYPE", "side": "long", "mode": "ENTRY",
         "verdict": "LONG-NOW"}, path=path)          # v1 entry, no system key
    all_s = audit_log.compute_summary(path=path)
    v2_s = audit_log.compute_summary(path=path, system="scan2")
    assert all_s["total_entries"] == 2
    assert v2_s["total_entries"] == 1
