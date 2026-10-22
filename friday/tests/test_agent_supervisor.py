from pathlib import Path

import pytest

from friday.services import agent_supervisor
from friday.services.agent_host import AgentHostLock
from friday.services.agent_supervisor import request_stop, run_supervisor


def test_request_stop_only_signals_an_active_supervisor(tmp_path):
    state_path = tmp_path / "tasks.jsonl"
    assert not request_stop(state_path)

    lock = AgentHostLock(state_path, suffix=".supervisor.lock")
    try:
        assert request_stop(state_path)
        assert Path(f"{state_path}.supervisor.stop").exists()
    finally:
        lock.close()


def test_supervisor_restarts_host_after_exit_and_stops_on_request(tmp_path, monkeypatch):
    state_path = tmp_path / "tasks.jsonl"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    stop_path = Path(f"{state_path}.supervisor.stop")
    launches = []

    class ExitedProcess:
        pid = 4321

        def __init__(self, command, **kwargs):
            launches.append((command, kwargs))

        def poll(self):
            return 1

    original_append_log = agent_supervisor._append_log

    def append_log(path, message):
        original_append_log(path, message)
        if "restarting in" in message:
            stop_path.write_text("stop", encoding="ascii")

    monkeypatch.setattr(agent_supervisor.subprocess, "Popen", ExitedProcess)
    monkeypatch.setattr(agent_supervisor, "_append_log", append_log)

    run_supervisor(state_path)

    assert len(launches) == 1
    assert launches[0][0][-2:] == ["-m", "friday.services.agent_host"]
    log = (tmp_path / "local" / "Friday" / "logs" / "agent-supervisor.log").read_text()
    assert "Agent host exited with code 1; restarting in 2s." in log
    assert "Supervisor stopped." in log
    assert not stop_path.exists()


def test_supervisor_is_single_instance(tmp_path):
    first = AgentHostLock(tmp_path / "tasks.jsonl", suffix=".supervisor.lock")
    try:
        with pytest.raises(RuntimeError, match="another persistent-agent host"):
            AgentHostLock(tmp_path / "tasks.jsonl", suffix=".supervisor.lock")
    finally:
        first.close()


def test_supervisor_does_not_spawn_a_second_host_for_the_same_store(tmp_path, monkeypatch):
    state_path = tmp_path / "tasks.jsonl"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    stop_path = Path(f"{state_path}.supervisor.stop")
    host_lock = AgentHostLock(state_path)
    original = agent_supervisor._append_log

    def append_log(path, message):
        original(path, message)
        if "Another agent host owns" in message:
            stop_path.write_text("stop", encoding="ascii")

    monkeypatch.setattr(agent_supervisor.subprocess, "Popen",
                        lambda *_args, **_kwargs: pytest.fail("duplicate host spawned"))
    monkeypatch.setattr(agent_supervisor, "_append_log", append_log)
    try:
        run_supervisor(state_path)
    finally:
        host_lock.close()

    assert "Another agent host owns this task store" in (
        tmp_path / "local" / "Friday" / "logs" / "agent-supervisor.log"
    ).read_text()
