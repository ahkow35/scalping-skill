import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import profile


def _p(tmp_path):
    return str(tmp_path / "profile.json")


def test_load_defaults_when_absent(tmp_path):
    prof = profile.load_profile(path=_p(tmp_path))
    assert prof["equity"] is None
    assert prof["phase"] == 1


def test_set_and_get_equity(tmp_path):
    p = _p(tmp_path)
    profile.set_field("equity", "5000", path=p)
    assert profile.load_profile(path=p)["equity"] == 5000.0
    assert profile.get_field("equity", path=p) == 5000.0


def test_set_phase(tmp_path):
    p = _p(tmp_path)
    profile.set_field("phase", "2", path=p)
    assert profile.load_profile(path=p)["phase"] == 2


def test_equity_must_be_positive(tmp_path):
    p = _p(tmp_path)
    with pytest.raises(ValueError):
        profile.set_field("equity", "-1", path=p)
    with pytest.raises(ValueError):
        profile.set_field("equity", "notnum", path=p)


def test_phase_must_be_1_or_2(tmp_path):
    p = _p(tmp_path)
    with pytest.raises(ValueError):
        profile.set_field("phase", "3", path=p)


def test_unknown_field_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown"):
        profile.set_field("leverage", "10", path=_p(tmp_path))


def test_default_risk_cap_by_phase():
    assert profile.default_risk_cap(1) == 0.5
    assert profile.default_risk_cap(2) == 2.0


def test_set_preserves_other_fields(tmp_path):
    p = _p(tmp_path)
    profile.set_field("equity", "8000", path=p)
    profile.set_field("phase", "2", path=p)
    prof = profile.load_profile(path=p)
    assert prof["equity"] == 8000.0 and prof["phase"] == 2
