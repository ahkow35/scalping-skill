"""Instruction-consistency lint — guards against the drift class that the
external review found (documented commands missing from the frontmatter
trigger list, stale admin-command counts). Deterministic; no model needed.
"""
import os
import re

SKILL = os.path.join(os.path.dirname(__file__), "..", "SKILL.md")

WORD_TO_INT = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
               "six": 6, "seven": 7}


def _read():
    with open(SKILL) as f:
        return f.read()


def _frontmatter(text):
    m = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
    assert m, "SKILL.md must open with a frontmatter block"
    return m.group(1)


def _admin_commands(text):
    # admin command subsections: ### `/scalp <cmd> ...`
    return sorted(set(re.findall(r"^###\s+`/scalp\s+([\w-]+)", text, re.M)))


def test_every_admin_command_is_in_frontmatter_triggers():
    text = _read()
    fm = _frontmatter(text)
    missing = [c for c in _admin_commands(text)
               if f"/scalp {c}" not in fm]
    assert not missing, f"admin commands missing from frontmatter triggers: {missing}"


def test_admin_command_count_word_matches():
    text = _read()
    fm = _frontmatter(text)
    n = len(_admin_commands(text))
    m = re.search(r"(\w+)\s+admin commands", fm)
    assert m, "frontmatter must state the admin-command count"
    stated = WORD_TO_INT.get(m.group(1).lower())
    assert stated == n, f"frontmatter says '{m.group(1)}' admin commands but {n} are documented"


def test_no_blanket_journal_stub_mandate():
    # the journal stub is action-verdict-only; no blanket 'must include' line
    text = _read()
    assert "Output must include a JOURNAL STUB block (ENTRY + MANAGE)" not in text, \
        "blanket JOURNAL STUB mandate contradicts no-action compact formats"


def test_core_documents_regime_weather_line():
    import os
    core = os.path.join(os.path.dirname(__file__), "..", "scalp-core.md")
    with open(core) as f:
        text = f.read()
    assert "regime" in text.lower()
    assert "WEATHER" in text
    assert "fade_ok" in text
