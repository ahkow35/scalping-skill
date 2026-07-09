"""Scalp profile — persisted account state the trading flow needs but cannot
infer: equity (for sizing math) and active phase (for the default risk cap).

Without this, every action verdict has to ask the user "what's your equity?"
or guess — which breaks /loop (no conversation to read, no one to ask). The
profile is a tiny JSON file the skill reads at sizing time.

Fields:
  equity — account equity in USDC (float > 0), or null if unset
  phase  — 1 or 2; the trading phase, which sets the default risk cap

CLI:
  python3 profile.py get
  python3 profile.py set equity 5000
  python3 profile.py set phase 2
"""

import json
import os
import sys


PROFILE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".scalp_profile.json")

DEFAULTS = {"equity": None, "phase": 1}
PHASE_RISK_CAP = {1: 0.5, 2: 2.0}  # percent of equity


def load_profile(path=None):
    path = path or PROFILE_PATH
    prof = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path) as f:
                prof.update({k: v for k, v in json.load(f).items()
                             if k in DEFAULTS})
        except (OSError, json.JSONDecodeError):
            pass
    return prof


def _coerce(field, raw):
    if field == "equity":
        val = float(raw)
        if val <= 0:
            raise ValueError("equity must be a positive number")
        return val
    if field == "phase":
        val = int(raw)
        if val not in PHASE_RISK_CAP:
            raise ValueError("phase must be 1 or 2")
        return val
    raise ValueError(f"unknown profile field: {field}")


def set_field(field, raw_value, path=None):
    path = path or PROFILE_PATH
    value = _coerce(field, raw_value)
    prof = load_profile(path)
    prof[field] = value
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(prof, f, indent=2)
    os.replace(tmp, path)
    return prof


def get_field(field, path=None):
    if field not in DEFAULTS:
        raise ValueError(f"unknown profile field: {field}")
    return load_profile(path)[field]


def default_risk_cap(phase):
    return PHASE_RISK_CAP[int(phase)]


def main(argv):
    if len(argv) < 2 or argv[1] not in ("get", "set"):
        print("usage: profile.py {get | set <field> <value>}", file=sys.stderr)
        return 2
    if argv[1] == "get":
        print(json.dumps(load_profile(), indent=2))
        return 0
    if len(argv) < 4:
        print("usage: profile.py set <field> <value>", file=sys.stderr)
        return 2
    try:
        prof = set_field(argv[2], argv[3])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(prof, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
