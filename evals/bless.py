"""Bless a captured market snapshot into a regression golden case.

Reads a snapshot (from the recorded firehose evals/snapshots.jsonl, or an
explicit fixture file), runs the CURRENT decide.decide() on it, and freezes the
result as the expected baseline in evals/golden/<id>.json. Committing that file
locks in today's behaviour; run_regression.py then flags any future divergence.

The behavioural state and profile are FROZEN INTO the case (default: clear
behavioural gate, equity $5000 / phase 1) so re-runs are deterministic and
independent of live profile drift.

Usage:
    python3 evals/bless.py --latest                 # newest snapshot, both sides
    python3 evals/bless.py --index -1 --sides long  # pick a snapshot line, one side
    python3 evals/bless.py --fixture /path/out.json --label smoke
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import decide  # noqa: E402

GOLDEN_DIR = os.path.join(HERE, "golden")
SNAPSHOTS = os.path.join(HERE, "snapshots.jsonl")

CLEAR_BEHAVIORAL = {"daily_stop": {"active": False}, "cooldown": {"active": False}}
DEFAULT_PROFILE = {"equity": 5000.0, "phase": 1}


def _load_snapshot(args):
    if args.fixture:
        market = json.load(open(args.fixture))
        return market, None
    if not os.path.exists(SNAPSHOTS):
        sys.exit(f"no snapshots yet at {SNAPSHOTS} — run fetch_market.py first")
    lines = [json.loads(ln) for ln in open(SNAPSHOTS) if ln.strip()]
    if args.coin:
        lines = [r for r in lines if r.get("coin") == args.coin]
    if not lines:
        sys.exit("no matching snapshots")
    rec = lines[-1] if args.latest else lines[args.index]
    return rec["market"], rec.get("ts_ms")


def bless(market, sides, ts_ms=None, label=None, profile=None, behavioral=None,
          golden_dir=GOLDEN_DIR):
    profile = profile or DEFAULT_PROFILE
    behavioral = behavioral or CLEAR_BEHAVIORAL
    os.makedirs(golden_dir, exist_ok=True)
    coin = market.get("primary", "COIN")
    stamp = label or (str(ts_ms) if ts_ms else "manual")
    written = []
    for side in sides:
        expected = decide.decide(side, market, behavioral, profile, params=None)
        case_id = f"{coin}-{stamp}-{side}".lower()
        case = {"id": case_id, "coin": coin, "side": side, "captured_ts_ms": ts_ms,
                "behavioral": behavioral, "profile": profile, "params": None,
                "market": market, "expected": expected}
        fp = os.path.join(golden_dir, f"{case_id}.json")
        with open(fp, "w") as f:
            json.dump(case, f, indent=2, default=str)
        written.append((fp, side, expected.get("verdict"), expected.get("conviction")))
    return written


def main(argv):
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group()
    src.add_argument("--latest", action="store_true", help="newest snapshot line")
    src.add_argument("--index", type=int, default=-1, help="snapshot line index")
    p.add_argument("--fixture", help="bless an explicit market JSON file instead")
    p.add_argument("--coin", help="filter snapshots by coin")
    p.add_argument("--sides", default="long,short", help="comma list: long,short")
    p.add_argument("--label", help="stamp used in the case id (else ts/manual)")
    args = p.parse_args(argv)

    market, ts_ms = _load_snapshot(args)
    sides = [s.strip() for s in args.sides.split(",") if s.strip()]
    written = bless(market, sides, ts_ms=ts_ms, label=args.label)
    for fp, side, verdict, conv in written:
        print(f"blessed {side:5s} -> {verdict} ({conv})  {os.path.basename(fp)}")
    print(f"\n{len(written)} case(s) written to {GOLDEN_DIR}")
    print("commit them, then edits to decide.py are guarded by evals/run_regression.py")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
