"""Scalp audit log — structured JSONL record of every QUICK/DEEP/MANAGE output.

Two-phase lifecycle:
  1. write_audit_entry(payload) at the moment a thesis or decision is produced.
     Returns a trade_id; the entry's `outcome` field is null.
  2. resolve_audit_entry(trade_id, outcome_r, exit_reason, lesson) when the
     trade closes (or the thesis is invalidated without entry). Fills `outcome`.

Why a separate file from fetch_market.py: the fetcher is data-only. The audit
log captures the LLM's *decision* (verdict, conviction, trigger choice) which
the fetcher never sees. Keeping these concerns separate makes both testable.

CLI usage:
  python3 audit_log.py log < <json-payload>
  python3 audit_log.py resolve <trade_id> <outcome_r> <exit_reason> [lesson]
  python3 audit_log.py list-open
  python3 audit_log.py summary [--since-days N]
"""

import json
import os
import sys
import time
from datetime import datetime, timezone


AUDIT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".audit_log.jsonl")


def _ts_ms():
    return int(time.time() * 1000)


def _utc_iso(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _generate_trade_id(coin, side, ts_ms, existing_ids):
    base = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y%m%dT%H%M%S")
    candidate = f"scalp-{base}-{coin.replace(':', '_')}-{side}"
    if candidate not in existing_ids:
        return candidate
    n = 2
    while f"{candidate}-{n}" in existing_ids:
        n += 1
    return f"{candidate}-{n}"


def _load_entries(path=None):
    path = path or AUDIT_LOG_PATH
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _write_entries(entries, path=None):
    path = path or AUDIT_LOG_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    os.replace(tmp, path)


REQUIRED_FIELDS = ("coin", "side", "mode", "verdict")


def write_audit_entry(payload, ts_ms=None, path=None):
    """Append a new audit row. Returns the generated trade_id.

    Required fields in payload: coin, side, mode, verdict. All other fields
    are passed through verbatim. `outcome` is forced to None at write time.
    """
    for required in REQUIRED_FIELDS:
        if required not in payload:
            raise ValueError(f"audit entry missing required field: {required}")

    ts_ms = ts_ms or _ts_ms()
    entries = _load_entries(path)
    existing_ids = {e.get("id") for e in entries}
    trade_id = _generate_trade_id(
        payload["coin"], payload["side"], ts_ms, existing_ids)

    # Strip caller-supplied id/ts/outcome — those are owned by this module.
    safe_payload = {k: v for k, v in payload.items()
                    if k not in ("id", "ts_ms", "ts_utc", "outcome")}

    entry = {
        "id": trade_id,
        "ts_ms": ts_ms,
        "ts_utc": _utc_iso(ts_ms),
        **safe_payload,
        "outcome": None,
    }
    entries.append(entry)
    _write_entries(entries, path)
    return trade_id


def resolve_audit_entry(trade_id, outcome_r, exit_reason, lesson=None,
                        resolved_at_ms=None, path=None):
    """Fill the outcome field on a previously-written entry.

    Returns True on success, False if trade_id not found or already resolved.
    Does NOT overwrite an existing outcome — caller must explicitly delete
    and re-create if a fix is needed (immutability by default).
    """
    resolved_at_ms = resolved_at_ms or _ts_ms()
    entries = _load_entries(path)
    for e in entries:
        if e.get("id") == trade_id:
            if e.get("outcome") is not None:
                return False
            e["outcome"] = {
                "resolved_at_ms": resolved_at_ms,
                "resolved_at_utc": _utc_iso(resolved_at_ms),
                "outcome_r": float(outcome_r),
                "exit_reason": exit_reason,
                "lesson": lesson,
            }
            _write_entries(entries, path)
            return True
    return False


ACTION_VERDICTS = ("LONG-NOW", "SHORT-NOW")


def attach_counterfactual(trade_id, counterfactual, path=None, force=False):
    """Attach a replay-engine counterfactual block to an entry.

    Returns True on success; False if trade_id not found or already has a
    counterfactual (unless force=True). Never touches `outcome` — a real
    trade result always lives separately and takes precedence.
    """
    entries = _load_entries(path)
    for e in entries:
        if e.get("id") == trade_id:
            if e.get("counterfactual") is not None and not force:
                return False
            e["counterfactual"] = counterfactual
            _write_entries(entries, path)
            return True
    return False


def list_open_entries(now_ms=None, path=None):
    """Entries still needing resolution, with an `age_h` annotation.

    An entry is open when `outcome` is null — EXCEPT non-action verdicts
    (WAIT/VETOED/...) that already carry a counterfactual: those are
    considered closed by replay (there was never a live position to resolve).
    Action verdicts always stay open until a real `resolve`.
    """
    now_ms = now_ms or _ts_ms()
    out = []
    for e in _load_entries(path):
        if e.get("outcome") is not None:
            continue
        if (e.get("verdict") not in ACTION_VERDICTS
                and e.get("counterfactual") is not None):
            continue
        out.append({**e, "age_h": round((now_ms - int(e["ts_ms"])) / 3_600_000, 1)})
    return out


def compute_summary(since_days=None, path=None, now_ms=None):
    """Aggregate stats across audit entries.

    Returns:
      total_entries, resolved, open
      verdict_distribution: {verdict: count}  (across ALL entries in window)
      by_setup: {setup_name: {n, win_rate, expectancy_r, total_r}}
                (across RESOLVED entries only)

    `setup_name` priority: payload.setup -> payload.trigger_used -> verdict.
    """
    now_ms = now_ms or _ts_ms()
    entries = _load_entries(path)
    if since_days is not None:
        cutoff_ms = now_ms - since_days * 86_400_000
        entries = [e for e in entries if int(e.get("ts_ms", 0)) >= cutoff_ms]

    verdict_counts = {}
    for e in entries:
        v = e.get("verdict", "UNKNOWN")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1

    resolved = [e for e in entries if e.get("outcome")]
    by_setup = {}
    for e in resolved:
        setup = e.get("setup") or e.get("trigger_used") or e.get("verdict", "UNKNOWN")
        r = float(e["outcome"]["outcome_r"])
        by_setup.setdefault(setup, []).append(r)

    setup_stats = {}
    for setup, rs in by_setup.items():
        n = len(rs)
        wins = sum(1 for r in rs if r > 0)
        setup_stats[setup] = {
            "n": n,
            "win_rate": round(wins / n, 3) if n else None,
            "expectancy_r": round(sum(rs) / n, 3) if n else None,
            "total_r": round(sum(rs), 3),
        }

    # "open" mirrors list_open_entries: counterfactually-closed non-action
    # rows are not open — only live positions / armed action verdicts are.
    open_n = sum(
        1 for e in entries
        if e.get("outcome") is None
        and not (e.get("verdict") not in ACTION_VERDICTS
                 and e.get("counterfactual") is not None))

    return {
        "total_entries": len(entries),
        "resolved": len(resolved),
        "open": open_n,
        "verdict_distribution": verdict_counts,
        "by_setup": setup_stats,
        "counterfactual": _counterfactual_stats(entries),
    }


def _counterfactual_stats(entries):
    """Gate-value aggregates over replay-scored entries.

    wait.missed_r  — R the WAITs sat out (positive = the gate cost you)
    veto.avoided_r — R the VETOs dodged (negative = the gate saved you)
    by_setup       — counterfactual expectancy per `<side>-<trigger label>`
    """
    scored = [e for e in entries if e.get("counterfactual")]

    def bucket(verdicts):
        rows = [e for e in scored if e.get("verdict") in verdicts]
        fired = [e for e in rows
                 if e["counterfactual"].get("best_r") is not None]
        return {
            "n": len(rows),
            "fired": len(fired),
            "total_best_r": round(sum(e["counterfactual"]["best_r"]
                                      for e in fired), 3),
        }

    wait = bucket(("WAIT",))
    veto = bucket(("VETOED", "NO-TRADE", "HALT"))

    by_setup = {}
    for e in scored:
        for label, res in (e["counterfactual"].get("per_trigger") or {}).items():
            if res.get("r") is None:
                continue
            by_setup.setdefault(f"{e.get('side')}-{label}", []).append(res["r"])
    setup_stats = {}
    for setup, rs in by_setup.items():
        setup_stats[setup] = {
            "n": len(rs),
            "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 3),
            "expectancy_r": round(sum(rs) / len(rs), 3),
            "total_r": round(sum(rs), 3),
        }

    return {
        "scored": len(scored),
        "wait": {"n": wait["n"], "fired": wait["fired"],
                 "missed_r": wait["total_best_r"]},
        "veto": {"n": veto["n"], "fired": veto["fired"],
                 "avoided_r": veto["total_best_r"]},
        "by_setup": setup_stats,
    }


def main(argv):
    if len(argv) < 2:
        print("usage: audit_log.py {log|resolve|list-open|summary} ...",
              file=sys.stderr)
        return 2
    cmd = argv[1]

    if cmd == "log":
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON on stdin: {exc}", file=sys.stderr)
            return 2
        try:
            trade_id = write_audit_entry(payload)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(trade_id)
        return 0

    if cmd == "resolve":
        if len(argv) < 5:
            print("usage: audit_log.py resolve <trade_id> <outcome_r> "
                  "<exit_reason> [lesson]", file=sys.stderr)
            return 2
        trade_id = argv[2]
        try:
            outcome_r = float(argv[3])
        except ValueError:
            print("outcome_r must be a number (R-multiple, e.g. -1, 2.5)",
                  file=sys.stderr)
            return 2
        exit_reason = argv[4]
        lesson = argv[5] if len(argv) > 5 else None
        ok = resolve_audit_entry(trade_id, outcome_r, exit_reason, lesson)
        if not ok:
            print(f"trade_id not found or already resolved: {trade_id}",
                  file=sys.stderr)
            return 1
        print(f"resolved {trade_id}: {outcome_r}R via {exit_reason}")
        return 0

    if cmd == "list-open":
        out = list_open_entries()
        print(json.dumps(out, indent=2, default=str))
        return 0

    if cmd == "summary":
        since = None
        if "--since-days" in argv:
            try:
                since = int(argv[argv.index("--since-days") + 1])
            except (ValueError, IndexError):
                print("--since-days requires an integer argument",
                      file=sys.stderr)
                return 2
        out = compute_summary(since_days=since)
        print(json.dumps(out, indent=2))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
