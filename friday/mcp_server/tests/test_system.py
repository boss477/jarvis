import pytest
from friday.mcp_server import system, safety


@pytest.fixture(autouse=True)
def reset():
    safety._reset()
    yield
    safety._reset()


class FakeEndpoint:
    def __init__(self):
        self.scalar = None
        self.muted = 0
    def SetMasterVolumeLevelScalar(self, v, _):
        self.scalar = v
    def GetMute(self):
        return self.muted
    def SetMute(self, v, _):
        self.muted = v


def test_set_volume_valid(monkeypatch):
    fake = FakeEndpoint()
    monkeypatch.setattr(system, "_endpoint", lambda: fake)
    msg = system.set_volume(50)
    assert fake.scalar == 0.5
    assert "50" in msg


def test_set_volume_out_of_range_rejected():
    assert "0-100" in system.set_volume(150)
    assert "0-100" in system.set_volume(-1)


def test_toggle_mute_flips(monkeypatch):
    fake = FakeEndpoint()
    monkeypatch.setattr(system, "_endpoint", lambda: fake)
    system.toggle_mute()
    assert fake.muted == 1


def test_set_brightness_invokes_powershell(monkeypatch):
    calls = {}
    monkeypatch.setattr(system.subprocess, "run", lambda args, **k: calls.setdefault("args", args))
    msg = system.set_brightness(70)
    assert any("WmiSetBrightness(1,70)" in str(a) for a in calls["args"])
    assert "70" in msg


def test_media_control_invalid_action():
    assert "play_pause" in system.media_control("rewind")


def test_power_action_stages_not_runs(monkeypatch):
    ran = {}
    monkeypatch.setattr(system.subprocess, "run", lambda *a, **k: ran.setdefault("ran", True))
    msg = system.power_action("shutdown")
    assert msg.startswith("CONFIRM_REQUIRED")
    assert "ran" not in ran  # nothing executed on first call


def test_power_action_invalid_mode():
    assert "restart" in system.power_action("explode")


def test_power_action_confirm_runs(monkeypatch):
    ran = {}
    monkeypatch.setattr(system.subprocess, "run", lambda *a, **k: ran.setdefault("ran", True))
    system.power_action("restart")
    safety.confirm_action(safety._pending.id)
    assert ran.get("ran") is True
