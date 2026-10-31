import io
import multiprocessing
from pathlib import Path
import threading

import pytest

from friday.services import agent_supervisor
from friday.services.agent_host import AgentHostLock
from friday.services.agent_supervisor import request_stop, run_supervisor


def _append_log_records_in_process(path, prefix, count):
    for index in range(count):
        agent_supervisor._append_log(Path(path), f"{prefix}-{index}")


def test_request_stop_only_signals_an_active_supervisor(tmp_path):
    state_path = tmp_path / "tasks.jsonl"
    assert not request_stop(state_path)

    lock = AgentHostLock(state_path, suffix=".supervisor.lock")
    try:
        assert request_stop(state_path)
        assert Path(f"{state_path}.supervisor.stop").exists()
    finally:
        lock.close()


def test_request_stop_clears_stale_marker_before_releasing_lock(tmp_path, monkeypatch):
    state_path = tmp_path / "tasks.jsonl"
    stop_path = Path(f"{state_path}.supervisor.stop")
    stop_path.write_text("stale", encoding="ascii")

    class FakeLock:
        held = False

        def __init__(self, *_args, **_kwargs):
            if FakeLock.held:
                raise RuntimeError("lock is already held")
            FakeLock.held = True

        def close(self):
            FakeLock.held = False

    original_unlink = Path.unlink
    observed_lock_state = []

    def checking_unlink(path, *args, **kwargs):
        if path == stop_path:
            observed_lock_state.append(FakeLock.held)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(agent_supervisor, "AgentHostLock", FakeLock)
    monkeypatch.setattr(Path, "unlink", checking_unlink)

    assert not request_stop(state_path)
    assert observed_lock_state == [True]
    assert not stop_path.exists()
    assert not FakeLock.held


def test_supervisor_restarts_host_after_exit_and_stops_on_request(tmp_path, monkeypatch):
    state_path = tmp_path / "tasks.jsonl"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    stop_path = Path(f"{state_path}.supervisor.stop")
    launches = []

    class ExitedProcess:
        pid = 4321

        def __init__(self, command, **kwargs):
            launches.append((command, kwargs))
            self.stdout = io.StringIO("host output line\n")

        def poll(self):
            return 1

    original_append_log = agent_supervisor._append_log

    def append_log(path, message):
        original_append_log(path, message)
        if "restarting in" in message:
            stop_path.write_text("stop", encoding="ascii")

    real_thread = threading.Thread
    daemon_values = []
    join_calls = []

    class TrackingThread:
        def __init__(self, *args, **kwargs):
            daemon_values.append(kwargs.get("daemon"))
            self.thread = real_thread(*args, **kwargs)

        def start(self):
            return self.thread.start()

        def join(self, *args, **kwargs):
            join_calls.append((args, kwargs))
            return self.thread.join(*args, **kwargs)

        def is_alive(self):
            return self.thread.is_alive()

    monkeypatch.setattr(agent_supervisor.threading, "Thread", TrackingThread)
    monkeypatch.setattr(agent_supervisor.subprocess, "Popen", ExitedProcess)
    monkeypatch.setattr(agent_supervisor, "_append_log", append_log)

    run_supervisor(state_path)

    assert len(launches) == 1
    assert launches[0][0][1:4] == ["-u", "-m", "friday.services.agent_host"]
    assert launches[0][1]["env"]["AGENT_TASKS_PATH"] == str(state_path.resolve())
    log = (tmp_path / "local" / "Friday" / "logs" / "agent-supervisor.log").read_text()
    assert "Agent host exited with code 1; restarting in 2s." in log
    assert "Supervisor stopped." in log
    assert "host: host output line" in log
    assert daemon_values == [False]
    assert join_calls == [((), {})]
    assert not stop_path.exists()


def test_supervisor_terminates_running_host_on_stop_request(tmp_path, monkeypatch):
    state_path = tmp_path / "tasks.jsonl"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    stop_path = Path(f"{state_path}.supervisor.stop")
    events = []

    class RunningProcess:
        pid = 4321
        returncode = None

        def __init__(self, command, **kwargs):
            self.polls = 0

        def poll(self):
            self.polls += 1
            if self.polls > 3 and self.returncode is None:
                self.returncode = 0
            return self.returncode

        def terminate(self):
            events.append("terminate")
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            events.append("kill")
            self.returncode = -9

    original_append_log = agent_supervisor._append_log

    def append_log(path, message):
        original_append_log(path, message)
        if "Started agent host process" in message:
            stop_path.write_text("stop", encoding="ascii")

    monkeypatch.setattr(agent_supervisor.subprocess, "Popen", RunningProcess)
    monkeypatch.setattr(agent_supervisor, "_append_log", append_log)

    run_supervisor(state_path)

    assert events == ["terminate"]
    log = (tmp_path / "local" / "Friday" / "logs" / "agent-supervisor.log").read_text()
    assert "Stop requested; terminating agent host." in log
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

def test_supervisor_log_rotates_and_bounds_total_size(tmp_path, monkeypatch):
    log_path = tmp_path / "agent-supervisor.log"
    monkeypatch.setattr(agent_supervisor, "_MAX_LOG_BYTES", 256)
    monkeypatch.setattr(agent_supervisor, "_MAX_LOG_MESSAGE_BYTES", 64)
    monkeypatch.setattr(agent_supervisor, "_LOG_BACKUP_COUNT", 2)

    # A stale extra backup must be counted (and pruned on rotation), while
    # the .mutex lock sidecar must not count as a log backup.
    log_path.with_name("agent-supervisor.log.3").write_text("stale backup")
    for index in range(20):
        agent_supervisor._append_log(log_path, f"record-{index}-" + "x" * 1000)

    backups = sorted(
        path for path in log_path.parent.glob(f"{log_path.name}.*")
        if path.name.rsplit(".", 1)[-1].isdigit()
    )
    files = [log_path, *backups]
    assert len(files) <= 3
    assert all(path.stat().st_size <= 256 for path in files)
    assert sum(path.stat().st_size for path in files) <= 3 * 256
    assert "[truncated]" in log_path.read_text(encoding="utf-8")
    assert log_path.with_name("agent-supervisor.log.1").exists()
    assert not log_path.with_name("agent-supervisor.log.3").exists()


def test_supervisor_log_rotation_failure_is_best_effort(tmp_path, monkeypatch):
    log_path = tmp_path / "agent-supervisor.log"
    monkeypatch.setattr(agent_supervisor, "_MAX_LOG_BYTES", 128)
    monkeypatch.setattr(agent_supervisor, "_MAX_LOG_MESSAGE_BYTES", 64)
    log_path.write_bytes(b"x" * 128)

    def fail_replace(_source, _target):
        raise PermissionError("file is temporarily in use")

    monkeypatch.setattr(agent_supervisor.os, "replace", fail_replace)

    agent_supervisor._append_log(log_path, "record after rotation failure")

    assert "record after rotation failure" in log_path.read_text(encoding="utf-8")


def test_supervisor_log_writes_are_serialized_across_processes(tmp_path):
    log_path = tmp_path / "agent-supervisor.log"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_append_log_records_in_process,
                        args=(str(log_path), f"worker-{worker}", 30))
        for worker in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 60
    assert sum("worker-0-" in line for line in lines) == 30
    assert sum("worker-1-" in line for line in lines) == 30


def test_supervisor_stream_splits_long_unterminated_output(tmp_path, monkeypatch):
    class TrackingStream(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.read_sizes = []

        def read1(self, size=-1):
            self.read_sizes.append(size)
            return super().read1(size)

    messages = []
    monkeypatch.setattr(agent_supervisor, "_append_log",
                        lambda _path, message: messages.append(message))
    # Put a multibyte character across the first read boundary to verify the
    # incremental decoder preserves UTF-8 while splitting a long line.
    long_line = "x" * 2047 + "🙂" + "x" * 2953
    stream = TrackingStream(("first\r\n" + long_line + "\nlast").encode("utf-8"))

    agent_supervisor._stream_host_output(stream, tmp_path / "agent-supervisor.log")

    assert stream.closed
    assert stream.read_sizes and set(stream.read_sizes) == {agent_supervisor._HOST_OUTPUT_READ_CHARS}
    assert messages[0] == "host: first"
    assert messages[-1] == "host: last"
    long_parts = [message[len("host: "):].removesuffix(" [continued]")
                  for message in messages[1:-1]]
    assert "".join(long_parts) == long_line
    assert all(len(message) <= agent_supervisor._HOST_OUTPUT_RECORD_CHARS + 24
               for message in messages)
