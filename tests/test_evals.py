# tests/test_evals.py — exercises the bless -> run_regression pipeline hermetically.
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "evals"))
sys.path.insert(0, os.path.join(HERE, ".."))
import bless  # noqa: E402
import run_regression  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "market_hype.json")


def _market():
    return json.load(open(FIXTURE))


def test_bless_writes_cases_for_each_side(tmp_path):
    written = bless.bless(_market(), ["long", "short"], ts_ms=111,
                          golden_dir=str(tmp_path))
    assert len(written) == 2
    ids = {json.load(open(fp))["side"] for fp, *_ in written}
    assert ids == {"long", "short"}


def test_freshly_blessed_set_passes(tmp_path):
    # Blessing freezes decide()'s current output; re-running must match exactly.
    bless.bless(_market(), ["long", "short"], ts_ms=222, golden_dir=str(tmp_path))
    results = run_regression.run(str(tmp_path))
    assert results, "expected golden cases to run"
    assert all(r["passed"] for r in results), [r["diffs"] for r in results if not r["passed"]]


def test_regression_detects_a_changed_verdict(tmp_path):
    written = bless.bless(_market(), ["long"], ts_ms=333, golden_dir=str(tmp_path))
    fp = written[0][0]
    case = json.load(open(fp))
    # Corrupt the frozen expectation as a stand-in for a decide.py behaviour change.
    case["expected"]["verdict"] = "LONG-NOW-BOGUS"
    json.dump(case, open(fp, "w"))
    results = run_regression.run(str(tmp_path))
    assert results[0]["passed"] is False
    assert any(p == "verdict" for p, _, _ in results[0]["diffs"])


def test_reason_text_is_ignored_by_diff(tmp_path):
    # A wording-only change to `reason` must NOT fail a regression.
    written = bless.bless(_market(), ["long"], ts_ms=444, golden_dir=str(tmp_path))
    fp = written[0][0]
    case = json.load(open(fp))
    case["expected"]["reason"] = "totally different wording, same logic"
    json.dump(case, open(fp, "w"))
    results = run_regression.run(str(tmp_path))
    assert results[0]["passed"] is True
