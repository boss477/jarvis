import pytest
from friday.mcp_server import shell, safety


@pytest.fixture(autouse=True)
def reset():
    safety._reset()
    yield
    safety._reset()


def test_run_powershell_stages_not_runs(monkeypatch):
    ran = {}
    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **k: ran.setdefault("ran", True))
    msg = shell.run_powershell("Get-Date")
    assert msg.startswith("CONFIRM_REQUIRED")
    assert "Get-Date" in msg
    assert "ran" not in ran


def test_run_powershell_empty_rejected():
    assert "non-empty" in shell.run_powershell("   ")


def test_run_powershell_confirm_executes(monkeypatch):
    class FakeProc:
        stdout = "hello"
        stderr = ""
        returncode = 0
    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **k: FakeProc())
    shell.run_powershell("echo hello")
    out = safety.confirm_action(safety._pending.id)
    assert "hello" in out
