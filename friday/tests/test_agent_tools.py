import pytest

from friday.services.agent_tools import _parse_bool


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
