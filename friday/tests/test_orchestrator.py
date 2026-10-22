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
    orchestrator.register_tool("send_email", StepPolicy("browser", requires_approval=True), lambda _: "sent")
    result = orchestrator.execute_next(task.id)
    assert result["status"] == "waiting_approval"
    assert tasks.get(task.id).status == TaskStatus.WAITING_APPROVAL


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
