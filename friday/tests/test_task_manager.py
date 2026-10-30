from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import threading
import pytest

from friday.services.task_manager import TaskManager, TaskStatus
from friday.services.task_commands import TaskCommandService


def test_task_manager_uses_shared_jsonl_lock(tmp_path, monkeypatch):
    from friday.services import task_manager

    original_lock = task_manager._jsonl_file_lock
    lock_paths = []

    @contextmanager
    def tracked_lock(path):
        lock_paths.append(path)
        with original_lock(path):
            yield

    monkeypatch.setattr(task_manager, "_jsonl_file_lock", tracked_lock)
    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    task = manager.create("send", [{"tool": "send"}])

    assert manager.get(task.id).prompt == "send"
    assert lock_paths == [path, path, path]


def _approve_task_in_process(path, task_id, barrier, outcomes):
    manager = TaskManager(path)
    barrier.wait(timeout=10)
    try:
        manager.approve(task_id)
        outcomes.put(True)
    except ValueError:
        outcomes.put(False)


def test_task_checkpoints_and_completes(tmp_path):
    manager = TaskManager(tmp_path / "tasks.jsonl")
    task = manager.create("open and type", [
        {"tool": "open_app", "args": {"name": "powerpoint"}},
        {"tool": "type_text", "args": {"text": "hello"}},
    ])

    manager.start(task.id)
    manager.checkpoint(task.id, success=True, result="opened")
    current = manager.get(task.id)
    assert current.current_step == 1
    assert current.status == TaskStatus.RUNNING
    manager.checkpoint(task.id, success=True, result="typed")
    assert manager.get(task.id).status == TaskStatus.COMPLETED


def test_pause_resume_state_survives_reload(tmp_path):
    path = tmp_path / "tasks.jsonl"
    task = TaskManager(path).create("long task", [{"tool": "x"}])
    manager = TaskManager(path)
    manager.pause(task.id)
    assert TaskManager(path).get(task.id).status == TaskStatus.PAUSED


def test_approval_gate_blocks_until_approved(tmp_path):
    manager = TaskManager(tmp_path / "tasks.jsonl")
    task = manager.create("send email", [{"tool": "send"}])
    manager.require_approval(task.id, "sending email")
    assert manager.get(task.id).status == TaskStatus.WAITING_APPROVAL
    manager.approve(task.id)
    assert manager.get(task.id).status == TaskStatus.RUNNING


def test_remote_task_commands(tmp_path):
    manager = TaskManager(tmp_path / "tasks.jsonl")
    task = manager.create("long task", [{"tool": "x"}])
    commands = TaskCommandService(manager)
    assert commands.check(task.id)["status"] == "queued"
    assert commands.resume(task.id)["status"] == "running"
    assert commands.pause(task.id)["status"] == "paused"
    assert commands.cancel(task.id)["status"] == "cancelled"


def test_concurrent_approval_is_consumed_once(tmp_path):
    manager = TaskManager(tmp_path / "tasks.jsonl")
    task = manager.create("send", [{"tool": "send"}])
    manager.require_approval(task.id, "send via WhatsApp")
    barrier = threading.Barrier(8)

    def approve():
        barrier.wait(timeout=2)
        try:
            manager.approve(task.id)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _index: approve(), range(8)))

    assert sum(outcomes) == 1
    assert manager.get(task.id).status == TaskStatus.RUNNING


def test_independent_managers_refresh_shared_jsonl_state(tmp_path):
    path = tmp_path / "shared-tasks.jsonl"
    composer_manager = TaskManager(path)
    host_manager = TaskManager(path)
    task = composer_manager.create("send WhatsApp update", [{"tool": "send"}])

    assert host_manager.get(task.id).status == TaskStatus.QUEUED
    host_manager.require_approval(task.id, "send via WhatsApp")
    assert composer_manager.get(task.id).status == TaskStatus.WAITING_APPROVAL


def test_two_managers_cannot_approve_the_same_task_twice(tmp_path):
    path = tmp_path / "shared-tasks.jsonl"
    first, second = TaskManager(path), TaskManager(path)
    task = first.create("send", [{"tool": "send"}])
    first.require_approval(task.id, "send via WhatsApp")
    barrier = threading.Barrier(2)

    def approve(manager):
        barrier.wait(timeout=2)
        try:
            manager.approve(task.id)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, (first, second)))

    assert sum(results) == 1
    assert first.get(task.id).status == TaskStatus.RUNNING


def test_separate_processes_cannot_approve_the_same_task_twice(tmp_path):
    path = tmp_path / "multiprocess-tasks.jsonl"
    manager = TaskManager(path)
    task = manager.create("send", [{"tool": "send"}])
    manager.require_approval(task.id, "send via WhatsApp")

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    outcomes = context.Queue()
    processes = [context.Process(target=_approve_task_in_process,
                                 args=(str(path), task.id, barrier, outcomes))
                 for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    assert sorted(outcomes.get(timeout=2) for _ in processes) == [False, True]
    assert TaskManager(path).get(task.id).status == TaskStatus.RUNNING


def test_recovery_marks_running_step_uncertain_without_replaying(tmp_path):
    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    task = manager.create("send a message", [
        {"tool": "prepare"},
        {"tool": "send", "approved": True},
    ])
    manager.start(task.id)
    manager.checkpoint(task.id, success=True, result="prepared")

    restarted_manager = TaskManager(path)
    assert restarted_manager.recover_interrupted_tasks() == [task.id]
    recovered = restarted_manager.get(task.id)
    assert recovered.status == TaskStatus.FAILED
    assert recovered.steps[0].status == "completed"
    assert recovered.steps[1].status == "interrupted"
    assert not recovered.steps[1].approved
    assert "outcome is uncertain" in recovered.steps[1].error
    assert restarted_manager.recover_interrupted_tasks() == []
    assert TaskManager(path).get(task.id).steps[1].status == "interrupted"
    with pytest.raises(ValueError, match="not paused or queued"):
        restarted_manager.resume(task.id)


def test_pause_rejects_recovered_failed_task_with_interrupted_step(tmp_path):
    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    task = manager.create("send a message", [{"tool": "send"}])
    manager.start(task.id)

    assert manager.recover_interrupted_tasks() == [task.id]
    recovered = manager.get(task.id)
    assert recovered.status == TaskStatus.FAILED
    assert recovered.steps[0].status == "interrupted"

    with pytest.raises(ValueError, match="not queued or running"):
        manager.pause(task.id)

    assert manager.get(task.id).status == TaskStatus.FAILED
    with pytest.raises(ValueError, match="not paused or queued"):
        manager.resume(task.id)
    assert manager.get(task.id).status == TaskStatus.FAILED


def test_recovery_preserves_tasks_waiting_for_user_approval(tmp_path):
    manager = TaskManager(tmp_path / "tasks.jsonl")
    task = manager.create("send a message", [{"tool": "send"}])
    manager.require_approval(task.id, "send via WhatsApp")

    assert manager.recover_interrupted_tasks() == []
    assert manager.get(task.id).status == TaskStatus.WAITING_APPROVAL


def test_load_ignores_only_an_interrupted_final_jsonl_record(tmp_path):
    path = tmp_path / "tasks.jsonl"
    task = TaskManager(path).create("keep me", [{"tool": "x"}])
    with path.open("ab") as handle:
        handle.write(b'{"id":')

    restarted = TaskManager(path)
    assert restarted.get(task.id) is not None
    restarted.create("append after recovery", [{"tool": "y"}])
    assert len(TaskManager(path).list()) == 2


def test_load_rejects_corrupt_jsonl_record_before_final_line(tmp_path):
    path = tmp_path / "tasks.jsonl"
    path.write_bytes(b'{"prompt":"keep","steps":[],"status":"queued"}\n'
                     b'not-json\n'
                     b'{"prompt":"last","steps":[],"status":"queued"}\n')

    with pytest.raises(ValueError, match=r"invalid JSONL record.*:2"):
        TaskManager(path)


@pytest.mark.parametrize("terminal_status", [
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
])
def test_terminal_task_status_cannot_be_overwritten(tmp_path, terminal_status):
    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    task = manager.create("send a message", [{"tool": "send"}])

    if terminal_status == TaskStatus.COMPLETED:
        manager.start(task.id)
        manager.checkpoint(task.id, success=True, result="sent")
    elif terminal_status == TaskStatus.FAILED:
        manager.start(task.id)
        manager.checkpoint(task.id, success=False, error="provider failed")
    else:
        manager.cancel(task.id)

    assert manager.get(task.id).status == terminal_status
    transitions = (
        lambda: manager.start(task.id),
        lambda: manager.cancel(task.id),
        lambda: manager.require_approval(task.id, "send via WhatsApp"),
    )
    for transition in transitions:
        with pytest.raises(ValueError, match="terminal task status"):
            transition()
        assert manager.get(task.id).status == terminal_status
        assert TaskManager(path).get(task.id).status == terminal_status
