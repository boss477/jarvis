from contextlib import contextmanager
import threading
import pytest

from friday.services.orchestrator import TaskOrchestrator, StepPolicy
from friday.services.task_manager import TaskManager, TaskStatus
from friday.services.workers import WorkerRegistry

def test_orchestrator_routes_and_checkpoints(tmp_path):
    tasks = TaskManager(tmp_path / "tasks.jsonl"); workers = WorkerRegistry()
    workers.register("momo", {"windows"}); workers.set_online("momo")
    task = tasks.create("open", [{"tool": "open_app", "args": {"name": "notepad"}}]); tasks.start(task.id)
    orchestrator = TaskOrchestrator(tasks, workers)
    orchestrator.register_tool("open_app", StepPolicy("windows"), lambda args: f"opened {args['name']}")
    result = orchestrator.execute_next(task.id)
    assert result["ok"] and tasks.get(task.id).status == TaskStatus.COMPLETED

def test_orchestrator_stages_approval(tmp_path):
    tasks = TaskManager(tmp_path / "tasks.jsonl"); workers = WorkerRegistry()
    workers.register("browser", {"browser"}); workers.set_online("browser")
    task = tasks.create("send", [{"tool": "send_email"}]); tasks.start(task.id)
    orchestrator = TaskOrchestrator(tasks, workers)
    calls = []
    orchestrator.register_tool("send_email", StepPolicy("browser", requires_approval=True),
                               lambda _: calls.append("sent") or "sent")
    result = orchestrator.execute_next(task.id)
    assert result["status"] == "waiting_approval"
    assert tasks.get(task.id).status == TaskStatus.WAITING_APPROVAL
    assert calls == []


def test_concurrent_advances_execute_a_side_effect_once(tmp_path):
    tasks = TaskManager(tmp_path / "tasks.jsonl")
    workers = WorkerRegistry(); workers.register("sender", {"messaging"}); workers.set_online("sender")
    task = tasks.create("send", [{"tool": "send"}]); tasks.start(task.id)
    orchestrator = TaskOrchestrator(tasks, workers)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def send(_args):
        calls.append("sent")
        entered.set()
        assert release.wait(timeout=2)
        return "sent"

    orchestrator.register_tool("send", StepPolicy("messaging"), send)
    results = []
    first = threading.Thread(target=lambda: results.append(orchestrator.execute_next(task.id)))
    second = threading.Thread(target=lambda: results.append(orchestrator.execute_next(task.id)))
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2); second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert calls == ["sent"]
    assert len(results) == 2
    assert all(result["status"] == "completed" for result in results)


def test_interrupted_failed_task_cannot_reexecute_its_side_effect(tmp_path):
    tasks = TaskManager(tmp_path / "tasks.jsonl")
    task = tasks.create("send", [{"tool": "send"}])
    tasks.start(task.id)
    tasks.recover_interrupted_tasks()
    orchestrator = TaskOrchestrator(tasks)
    calls = []
    orchestrator.register_tool("send", StepPolicy("messaging"),
                               lambda _args: calls.append("sent") or "sent")

    result = orchestrator.execute_next(task.id)

    assert result["status"] == "failed"
    assert calls == []


@pytest.mark.parametrize(
    ("control", "expected_status"),
    [("pause", TaskStatus.PAUSED), ("cancel", TaskStatus.CANCELLED)],
)
def test_pause_or_cancel_during_step_preserves_control_and_stops_next_step(
        tmp_path, control, expected_status):
    tasks = TaskManager(tmp_path / f"{control}-tasks.jsonl")
    task = tasks.create("two sends", [
        {"tool": "send", "args": {"step": 1}},
        {"tool": "send", "args": {"step": 2}},
    ])
    tasks.start(task.id)
    workers = WorkerRegistry(); workers.register("sender", {"messaging"}); workers.set_online("sender")
    orchestrator = TaskOrchestrator(tasks, workers)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def send(args):
        calls.append(args["step"])
        if args["step"] == 1:
            entered.set()
            assert release.wait(timeout=2)
        return f"sent {args['step']}"

    orchestrator.register_tool("send", StepPolicy("messaging"), send)
    results = []
    thread = threading.Thread(target=lambda: results.append(orchestrator.execute_next(task.id)))
    thread.start()
    assert entered.wait(timeout=2)
    getattr(tasks, control)(task.id)
    release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    saved = tasks.get(task.id)
    assert saved.status == expected_status
    assert saved.current_step == 1
    assert saved.steps[0].result == "sent 1"
    assert calls == [1]
    assert results[0]["status"] == expected_status.value
    assert orchestrator.execute_next(task.id)["status"] == expected_status.value
    assert calls == [1]

    if control == "pause":
        tasks.resume(task.id)
        assert orchestrator.execute_next(task.id)["status"] == "completed"
        assert calls == [1, 2]


def test_cancel_before_durable_step_claim_prevents_side_effect(tmp_path, monkeypatch):
    tasks = TaskManager(tmp_path / "tasks.jsonl")
    workers = WorkerRegistry(); workers.register("sender", {"messaging"}); workers.set_online("sender")
    task = tasks.create("send", [{"tool": "send"}])
    tasks.start(task.id)
    orchestrator = TaskOrchestrator(tasks, workers)
    calls = []
    orchestrator.register_tool("send", StepPolicy("messaging"),
                               lambda _args: calls.append("sent") or "sent")
    choose_worker = workers.choose

    def cancel_before_claim(capability):
        worker = choose_worker(capability)
        tasks.cancel(task.id)
        return worker

    monkeypatch.setattr(workers, "choose", cancel_before_claim)

    result = orchestrator.execute_next(task.id)

    assert result["status"] == "cancelled"
    assert calls == []
    assert tasks.get(task.id).steps[0].status == "pending"


def test_checkpoint_failure_does_not_reclassify_successful_side_effect(tmp_path, monkeypatch):
    tasks = TaskManager(tmp_path / "tasks.jsonl")
    workers = WorkerRegistry()
    workers.register("sender", {"messaging"})
    workers.set_online("sender")
    task = tasks.create("send", [{"tool": "send"}])
    tasks.start(task.id)
    orchestrator = TaskOrchestrator(tasks, workers)
    sent = []
    orchestrator.register_tool(
        "send", StepPolicy("messaging"), lambda _args: sent.append("sent") or "sent"
    )

    original_persist = tasks._persist

    def fail_completed_checkpoint(saved_task):
        if saved_task.status == TaskStatus.COMPLETED:
            raise OSError("disk full")
        return original_persist(saved_task)

    checkpoint_results = []
    original_checkpoint = tasks.checkpoint

    def track_checkpoint(task_id, *, success, result="", error=""):
        checkpoint_results.append(success)
        return original_checkpoint(task_id, success=success, result=result, error=error)

    monkeypatch.setattr(tasks, "_persist", fail_completed_checkpoint)
    monkeypatch.setattr(tasks, "checkpoint", track_checkpoint)

    result = orchestrator.execute_next(task.id)
    repeated = orchestrator.execute_next(task.id)

    assert result["status"] == "checkpoint_failed"
    assert repeated["status"] == "checkpoint_failed"
    assert "disk full" in result["error"]
    assert sent == ["sent"]
    assert checkpoint_results == [True]



def test_recovery_waits_for_active_orchestrator_side_effect(tmp_path, monkeypatch):
    from friday.services import task_manager as task_manager_module

    path = tmp_path / "tasks.jsonl"
    tasks = TaskManager(path)
    task = tasks.create("send", [{"tool": "send"}])
    tasks.start(task.id)
    workers = WorkerRegistry()
    workers.register("sender", {"messaging"})
    workers.set_online("sender")
    orchestrator = TaskOrchestrator(tasks, workers)
    entered = threading.Event()
    release = threading.Event()
    execution_result = {}
    recovery_attempted = threading.Event()
    recovery_done = threading.Event()
    recovery_result = {}

    def send(_args):
        entered.set()
        assert release.wait(timeout=5)
        return "sent"

    orchestrator.register_tool("send", StepPolicy("messaging"), send)
    execution = threading.Thread(
        target=lambda: execution_result.setdefault(
            "result", orchestrator.execute_next(task.id)
        )
    )
    execution.start()
    assert entered.wait(timeout=2)

    original_lock = task_manager_module.task_execution_lock

    @contextmanager
    def track_recovery_lock(lock_path, task_id):
        if task_id == task.id:
            recovery_attempted.set()
        with original_lock(lock_path, task_id):
            yield

    monkeypatch.setattr(task_manager_module, "task_execution_lock", track_recovery_lock)

    def recover():
        recovery_result["recovered"] = TaskManager(path).recover_interrupted_tasks()
        recovery_done.set()

    recovery = threading.Thread(target=recover)
    recovery.start()
    try:
        assert recovery_attempted.wait(timeout=2)
        assert not recovery_done.wait(timeout=0.25)
    finally:
        release.set()

    execution.join(timeout=5)
    recovery.join(timeout=5)
    assert not execution.is_alive()
    assert not recovery.is_alive()
    assert execution_result["result"]["status"] == "completed"
    assert recovery_result["recovered"] == []
    assert TaskManager(path).get(task.id).status == TaskStatus.COMPLETED
