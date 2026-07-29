"""Regression runner for the scalp decision engine.

Replays every blessed golden case in evals/golden/*.json through the CURRENT
decide.decide() and diffs the result against the frozen `expected` output.

WHAT THIS CATCHES: unintended behaviour changes in decide.py / structure.py /
triggers.py / costs.py / threshold edits. The golden `expected` is whatever
decide() produced when the case was blessed, so any later divergence on a fixed
input is flagged. A diff is either a bug (fix it) or an intended change
(re-bless the case: `python3 evals/bless.py ...` and commit, noting why).

WHAT THIS DOES NOT COVER: the live interactive /scalp verdict is the LLM
reasoning through the markdown, NOT decide(). decide() is the deterministic port
of that logic; this harness regression-tests the port. LLM-vs-engine divergence
is a separate, future eval.

Human-text fields (`reason`, nested `reason`) are ignored — logic lives in the
structured fields; wording edits should not fail a regression.

Usage:
    python3 evals/run_regression.py            # run, print, exit 1 on any FAIL
    python3 evals/run_regression.py --quiet    # summary line only
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import decide  # noqa: E402

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
IGNORE_KEYS = {"reason"}


def _diff(expected, actual, path=""):
    """Deep diff → list of (dotted_path, expected, actual). Ignores IGNORE_KEYS."""
    diffs = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in expected.keys() | actual.keys():
            if k in IGNORE_KEYS:
                continue
            sub = f"{path}.{k}" if path else k
            if k not in expected:
                diffs.append((sub, "<absent>", actual[k]))
            elif k not in actual:
                diffs.append((sub, expected[k], "<absent>"))
            else:
                diffs.extend(_diff(expected[k], actual[k], sub))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            diffs.append((f"{path}[len]", len(expected), len(actual)))
        else:
            for i, (e, a) in enumerate(zip(expected, actual)):
                diffs.extend(_diff(e, a, f"{path}[{i}]"))
    else:
        if expected != actual:
            diffs.append((path, expected, actual))
    return diffs


def run(golden_dir=GOLDEN_DIR):
    """Run every golden case. Returns list of {id, side, passed, diffs}."""
    results = []
    for fp in sorted(glob.glob(os.path.join(golden_dir, "*.json"))):
        case = json.load(open(fp))
        actual = decide.decide(
            case["side"], case["market"], case["behavioral"], case["profile"],
            params=case.get("params"))
        diffs = _diff(case["expected"], actual)
        results.append({"id": case.get("id", os.path.basename(fp)),
                        "side": case["side"], "passed": not diffs, "diffs": diffs})
    return results


def main(argv):
    quiet = "--quiet" in argv
    results = run()
    if not results:
        print("no golden cases in evals/golden/ — bless some with evals/bless.py")
        return 0
    failed = [r for r in results if not r["passed"]]
    if not quiet:
        for r in results:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"  [{mark}] {r['id']} ({r['side']})")
            for path, exp, act in r["diffs"][:8]:
                print(f"         {path}: expected {exp!r} got {act!r}")
    print(f"\nSCALP REGRESSION: {len(results) - len(failed)}/{len(results)} passed"
          + (f" — {len(failed)} FAILED" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
