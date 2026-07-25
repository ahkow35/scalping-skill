import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(REPO, "skill", "scalp2", "SKILL.md")


def test_skill_and_protocol_agree_on_constants():
    import card2
    prose = open(os.path.join(REPO, "scalp2.md")).read()
    assert f"{card2.TIME_STOP_MIN}-min" in prose.replace("60 min", "60-min")
    assert "0.5%" in prose and "10x" in prose
    assert str(card2.EVIDENCE_MIN_RESOLVED) in prose


def test_skill_file_exists_and_is_thin():
    text = open(SKILL).read()
    assert len(text.splitlines()) <= 150
    assert "scan2.py" in text and "scalp2.md" in text
    assert re.search(r"never (upgrade|widen)", text, re.I) \
        or "veto/downgrade only" in text
