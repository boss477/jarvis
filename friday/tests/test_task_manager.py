from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from queue import Empty
from pathlib import Path
import hashlib
import threading
import pytest

from friday.services.task_manager import (
    TaskManager, TaskStatus, _task_execution_lock_path, task_execution_lock,
)
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


def test_task_execution_lock_is_reentrant_for_same_thread(tmp_path):
    path = tmp_path / "tasks.jsonl"
    entered = threading.Event()
    errors = []

    def nested_lock():
        try:
            with task_execution_lock(path, "nested-task"):
                with task_execution_lock(path, "nested-task"):
                    entered.set()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=nested_lock, daemon=True)
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive(), "nested task lock deadlocked"
    assert entered.is_set()
    assert errors == []


def test_task_execution_lock_ignores_cleanup_oserror(tmp_path, monkeypatch):
    from friday.services import task_manager

    def fail_cleanup(_path):
        raise OSError("registry lock unavailable")

    monkeypatch.setattr(task_manager, "_cleanup_task_execution_lock_sidecars", fail_cleanup)
    with task_execution_lock(tmp_path / "tasks.jsonl", "cleanup-task"):
        pass


def test_task_execution_lock_cleanup_error_does_not_mask_body_exception(tmp_path, monkeypatch):
    from friday.services import task_manager

    def fail_cleanup(_path):
        raise OSError("registry lock unavailable")

    monkeypatch.setattr(task_manager, "_cleanup_task_execution_lock_sidecars", fail_cleanup)
    with pytest.raises(ValueError, match="body failed"):
        with task_execution_lock(tmp_path / "tasks.jsonl", "exception-task"):
            raise ValueError("body failed")


def test_task_execution_lock_uses_bounded_sidecars(tmp_path):
    path = tmp_path / "tasks.jsonl"

    for index in range(256):
        with task_execution_lock(path, f"task-{index}"):
            pass

    sidecars = list(tmp_path.glob("tasks.jsonl.execution.*.mutex"))
    assert 0 < len(sidecars) <= 64


def _legacy_stripe_collision():
    buckets = {}
    for index in range(1024):
        task_id = f"task-{index}"
        digest = hashlib.sha256(task_id.encode("utf-8")).digest()
        stripe = int.from_bytes(digest[:4], "big") % 64
        if stripe in buckets:
            return buckets[stripe], task_id
        buckets[stripe] = task_id
    raise AssertionError("could not find a legacy stripe collision")


def test_task_execution_lock_does_not_serialize_legacy_stripe_collisions(tmp_path):
    path = tmp_path / "tasks.jsonl"
    first_id, second_id = _legacy_stripe_collision()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    errors = []

    def hold_first():
        try:
            with task_execution_lock(path, first_id):
                first_entered.set()
                if not release_first.wait(timeout=5):
                    errors.append("first lock was not released")
        except Exception as exc:
            errors.append(exc)

    def acquire_second():
        try:
            with task_execution_lock(path, second_id):
                second_entered.set()
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=hold_first)
    second = threading.Thread(target=acquire_second)
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    try:
        assert second_entered.wait(timeout=2)
    finally:
        release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert _task_execution_lock_path(path, first_id) != _task_execution_lock_path(path, second_id)


def test_task_execution_lock_cleanup_preserves_active_sidecars(tmp_path):
    path = tmp_path / "tasks.jsonl"
    active_id = "active-task"
    active_sidecar = Path(f"{_task_execution_lock_path(path, active_id)}.mutex")

    with task_execution_lock(path, active_id):
        for index in range(100):
            with task_execution_lock(path, f"other-{index}"):
                pass
        assert active_sidecar.exists()

    sidecars = list(tmp_path.glob("tasks.jsonl.execution.*.mutex"))
    assert len(sidecars) <= 64


def test_task_execution_lock_retries_after_contention(tmp_path, monkeypatch):
    from friday.services import task_manager

    original_try_lock = task_manager._try_jsonl_file_lock
    attempts = []

    def contended_once(path):
        attempts.append(path)
        if len(attempts) == 1:
            return None
        return original_try_lock(path)

    monkeypatch.setattr(task_manager, "_try_jsonl_file_lock", contended_once)
    with task_execution_lock(tmp_path / "tasks.jsonl", "task-1"):
        pass

    assert len(attempts) >= 2



def _approve_task_in_process(path, task_id, barrier, outcomes):
    manager = TaskManager(path)
    barrier.wait(timeout=10)
    try:
        manager.approve(task_id)
        outcomes.put(True)
    except ValueError:
        outcomes.put(False)


def _recover_task_in_process(path, started, outcomes):
    manager = TaskManager(path)
    started.set()
    outcomes.put(manager.recover_interrupted_tasks())


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
    assert manager.claim_step(task.id, 1)

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
    assert manager.claim_step(task.id, 0)

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


def test_recovery_query_exposes_safe_unclaimed_steps_to_the_origin_conversation(tmp_path):
    manager = TaskManager(tmp_path / "tasks.jsonl")
    task = manager.create(
        "send a message",
        [{"tool": "send"}],
        origin_channel="whatsapp",
        origin_sender_id="user-1",
        origin_conversation_id="chat-1",
    )
    manager.start(task.id)
    manager.recover_interrupted_tasks()

    commands = TaskCommandService(manager)
    recovered = commands.recovery_required("whatsapp", "user-1", "chat-1")

    assert [item["id"] for item in recovered] == [task.id]
    assert recovered[0]["status"] == "paused"
    assert recovered[0]["recovery_safe_to_resume"] is True
    assert recovered[0]["recovery_required"] is False
    assert commands.recovery_required("whatsapp", "user-1", "other-chat") == []


def test_recovery_pauses_unclaimed_step_and_allows_resume(tmp_path):
    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    task = manager.create("send a message", [{"tool": "send"}])
    manager.start(task.id)

    restarted = TaskManager(path)
    assert restarted.recover_interrupted_tasks() == [task.id]
    recovered = restarted.get(task.id)
    assert recovered.status == TaskStatus.PAUSED
    assert recovered.steps[0].status == "pending"
    assert recovered.steps[0].error == ""

    assert restarted.resume(task.id).status == TaskStatus.RUNNING
    assert restarted.claim_step(task.id, 0)



@pytest.mark.parametrize(
    ("interrupted_status", "expected_status"),
    [
        (TaskStatus.PAUSED, TaskStatus.FAILED),
        (TaskStatus.CANCELLED, TaskStatus.CANCELLED),
    ],
)
def test_recovery_quarantines_in_flight_steps_after_pause_or_cancel(
    tmp_path, interrupted_status, expected_status
):
    path = tmp_path / f"tasks-{interrupted_status.value}.jsonl"
    manager = TaskManager(path)
    task = manager.create("send a message", [{"tool": "send", "approved": True}])
    manager.start(task.id)
    assert manager.claim_step(task.id, 0)

    if interrupted_status == TaskStatus.PAUSED:
        manager.pause(task.id)
    else:
        manager.cancel(task.id)

    restarted = TaskManager(path)
    assert restarted.recover_interrupted_tasks() == [task.id]
    recovered = restarted.get(task.id)
    assert recovered.status == expected_status
    assert recovered.steps[0].status == "interrupted"
    assert not recovered.steps[0].approved
    assert "outcome is uncertain" in recovered.steps[0].error
    assert restarted.summary(task.id)["recovery_required"] is True
    assert restarted.recover_interrupted_tasks() == []

    with pytest.raises(ValueError, match="not paused or queued"):
        restarted.resume(task.id)

def test_recovery_only_locks_tasks_with_recoverable_statuses(tmp_path, monkeypatch):
    from friday.services import task_manager as task_manager_module

    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    queued = manager.create("queued", [{"tool": "send"}])
    waiting = manager.create("waiting", [{"tool": "send"}])
    completed = manager.create("completed", [{"tool": "send"}])
    failed = manager.create("failed", [{"tool": "send"}])
    running = manager.create("running", [{"tool": "send"}])
    paused = manager.create("paused", [{"tool": "send"}])
    cancelled = manager.create("cancelled", [{"tool": "send"}])

    manager.require_approval(waiting.id, "send")
    manager.start(completed.id)
    manager.checkpoint(completed.id, success=True, result="sent")
    manager.start(failed.id)
    manager.checkpoint(failed.id, success=False, error="provider failed")
    manager.start(running.id)
    manager.start(paused.id)
    manager.pause(paused.id)
    manager.start(cancelled.id)
    manager.cancel(cancelled.id)

    original_lock = task_manager_module.task_execution_lock
    locked_ids = []

    @contextmanager
    def tracking_lock(lock_path, task_id):
        locked_ids.append(task_id)
        with original_lock(lock_path, task_id):
            yield

    monkeypatch.setattr(task_manager_module, "task_execution_lock", tracking_lock)

    manager.recover_interrupted_tasks()

    assert set(locked_ids) == {running.id, paused.id, cancelled.id}
    assert queued.id not in locked_ids
    assert waiting.id not in locked_ids
    assert completed.id not in locked_ids
    assert failed.id not in locked_ids


def test_recovery_waits_for_active_task_execution_lock(tmp_path):
    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    task = manager.create("send a message", [{"tool": "send"}])
    manager.start(task.id)
    assert manager.claim_step(task.id, 0)

    context = multiprocessing.get_context("spawn")
    started = context.Event()
    outcomes = context.Queue()
    process = context.Process(
        target=_recover_task_in_process,
        args=(str(path), started, outcomes),
    )

    with task_execution_lock(path, task.id):
        process.start()
        assert started.wait(timeout=10)
        # A separate process must wait for the active task's execution lock.
        with pytest.raises(Empty):
            outcomes.get(timeout=0.25)
        manager.checkpoint(task.id, success=True, result="sent")

    process.join(timeout=15)
    assert not process.is_alive()
    assert process.exitcode == 0
    assert outcomes.get(timeout=2) == []
    assert TaskManager(path).get(task.id).status == TaskStatus.COMPLETED

def test_task_manager_compacts_append_only_history(tmp_path):
    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    task = manager.create("repeat state changes", [{"tool": "send"}])

    for _ in range(140):
        manager.start(task.id)
        manager.pause(task.id)

    # 281 historical rows are reduced to the latest task snapshot plus the
    # updates written since the most recent compaction.
    assert len(path.read_text(encoding="utf-8").splitlines()) < 256
    reloaded = TaskManager(path)
    assert len(reloaded.list()) == 1
    assert reloaded.get(task.id).status == TaskStatus.PAUSED
    assert reloaded.get(task.id).prompt == "repeat state changes"


def test_task_manager_compaction_preserves_latest_state_for_multiple_tasks(tmp_path):
    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    tasks = [
        manager.create(f"task {index}", [{"tool": "send"}])
        for index in range(3)
    ]

    for _ in range(45):
        for task in tasks:
            manager.start(task.id)
            manager.pause(task.id)

    assert len(path.read_text(encoding="utf-8").splitlines()) < 256
    reloaded = TaskManager(path)
    assert len(reloaded.list()) == len(tasks)
    for task in tasks:
        restored = reloaded.get(task.id)
        assert restored is not None
        assert restored.prompt == task.prompt
        assert restored.status == TaskStatus.PAUSED


def test_task_manager_compaction_failure_does_not_fail_persistence(tmp_path, monkeypatch):
    from friday.services import task_manager as task_manager_module

    path = tmp_path / "tasks.jsonl"
    manager = TaskManager(path)
    task = manager.create("keep updates durable", [{"tool": "send"}])

    def fail_replace(_source, _target):
        raise OSError("temporary file is locked")

    monkeypatch.setattr(task_manager_module.os, "replace", fail_replace)
    for _ in range(130):
        manager.start(task.id)
        manager.pause(task.id)

    restored = TaskManager(path).get(task.id)
    assert restored is not None
    assert restored.status == TaskStatus.PAUSED
