import os
from pathlib import Path

import pytest

from friday.services.agent_tools import _agent_file_path, _parse_bool


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        (" TRUE ", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
        (None, False),
        ("maybe", False),
    ],
)
def test_parse_bool_is_explicit(value, expected):
    assert _parse_bool(value) is expected



def test_default_file_search_uses_first_configured_root(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("AGENT_FILE_ROOTS", os.pathsep.join((str(first), str(second))))

    assert _agent_file_path(None) == first.resolve()
    assert _agent_file_path(str(second / "notes.txt")) == (second / "notes.txt").resolve()

    with pytest.raises(ValueError, match="outside the configured agent file roots"):
        _agent_file_path(str(tmp_path / "outside.txt"))
