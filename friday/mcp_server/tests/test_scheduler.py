from types import SimpleNamespace

import pytest

from friday.mcp_server import scheduler, safety


@pytest.fixture(autouse=True)
def reset():
    safety._reset()
    yield
    safety._reset()


def _fake_run(monkeypatch, returncode=0, stdout="", stderr=""):
    calls = {}
    def run(args, **k):
        calls["args"] = args
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(scheduler.subprocess, "run", run)
    return calls


def test_schedule_stages_not_runs(monkeypatch):
    calls = _fake_run(monkeypatch)
    msg = scheduler.schedule_task("wake me", "notepad", "14:30")
    assert msg.startswith("CONFIRM_REQUIRED")
    assert "args" not in calls  # nothing ran before confirm


def test_schedule_confirm_runs_with_prefix(monkeypatch):
    calls = _fake_run(monkeypatch)
    scheduler.schedule_task("wake me", "notepad", "14:30")
    out = safety.confirm_action(safety._pending.id)
    assert "Friday_wake_me" in calls["args"]
    assert "Scheduled" in out


def test_schedule_bad_time():
    assert "14:30" in scheduler.schedule_task("x", "notepad", "2pm")


def test_schedule_requires_fields():
    assert "required" in scheduler.schedule_task("", "notepad", "14:30")


def test_list_filters_to_friday(monkeypatch):
    _fake_run(monkeypatch, stdout=(
        "TaskName: \\Friday_wake_me\n"
        "TaskName: \\Microsoft\\SomeSystemTask\n"
        "TaskName: \\Friday_backup\n"
    ))
    out = scheduler.list_scheduled_tasks()
    assert "Friday_wake_me" in out and "Friday_backup" in out
    assert "SomeSystemTask" not in out


def test_list_empty(monkeypatch):
    _fake_run(monkeypatch, stdout="TaskName: \\Microsoft\\Other\n")
    assert "No Friday tasks" in scheduler.list_scheduled_tasks()


def test_delete_prefixes_name(monkeypatch):
    calls = _fake_run(monkeypatch)
    scheduler.delete_scheduled_task("wake me")
    assert "Friday_wake_me" in calls["args"]
