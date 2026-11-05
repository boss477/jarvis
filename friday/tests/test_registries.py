import pytest

from friday.services.agent_registry import AgentRegistry
from friday.services.connectors import ConnectorRegistry


def test_agent_registry_registers_and_requires_named_agents():
    registry = AgentRegistry()
    agent = object()
    assert registry.register("planner", agent) is agent
    assert registry.get("planner") is agent
    assert registry.require("planner") is agent
    assert registry.names() == ("planner",)
    assert registry.unregister("planner") is agent
    assert registry.get("planner") is None
    with pytest.raises(LookupError, match="agent not registered"):
        registry.require("planner")


def test_connector_registry_allows_generic_use_and_checks_specific_capabilities():
    registry = ConnectorRegistry()
    client = object()
    connector = registry.register("mail", {"send"}, client=client)
    assert registry.require("mail", "use") is connector
    assert registry.require("mail", "send").client is client
    with pytest.raises(ValueError, match="capability must be explicit"):
        registry.require("mail")
    with pytest.raises(PermissionError, match="does not grant capability 'delete'"):
        registry.require("mail", "delete")


def test_connector_registry_rejects_missing_or_disabled_connectors():
    registry = ConnectorRegistry()
    with pytest.raises(LookupError, match="connector not registered"):
        registry.require("mail")
    registry.register("mail", set()).enabled = False
    with pytest.raises(LookupError, match="connector is disabled"):
        registry.require("mail")

def test_orchestrator_checks_connector_capability_before_side_effect(tmp_path):
    from friday.services.orchestrator import StepPolicy, TaskOrchestrator
    from friday.services.task_manager import TaskManager
    from friday.services.workers import WorkerRegistry

    tasks = TaskManager(tmp_path / "tasks.jsonl")
    workers = WorkerRegistry()
    workers.register("sender", {"messaging"})
    workers.set_online("sender")
    connectors = ConnectorRegistry()
    connectors.register("mail", {"read"})
    task = tasks.create("send", [{"tool": "send", "args": {}}])
    tasks.start(task.id)
    calls = []
    orchestrator = TaskOrchestrator(tasks, workers, connectors)
    orchestrator.register_tool(
        "send",
        StepPolicy("messaging", connector="mail", connector_capability="send"),
        lambda _args: calls.append("sent"),
    )

    with pytest.raises(PermissionError, match="does not grant capability 'send'"):
        orchestrator.execute_next(task.id)

    assert calls == []
    assert tasks.get(task.id).steps[0].status == "pending"

def test_orchestrator_rejects_missing_connector_capability_before_side_effect(tmp_path):
    from friday.services.orchestrator import StepPolicy, TaskOrchestrator
    from friday.services.task_manager import TaskManager
    from friday.services.workers import WorkerRegistry

    tasks = TaskManager(tmp_path / "tasks.jsonl")
    workers = WorkerRegistry()
    workers.register("sender", {"messaging"})
    workers.set_online("sender")
    connectors = ConnectorRegistry()
    connectors.register("mail", {"read"})
    task = tasks.create("send", [{"tool": "send", "args": {}}])
    tasks.start(task.id)
    calls = []
    orchestrator = TaskOrchestrator(tasks, workers, connectors)
    orchestrator.register_tool(
        "send",
        StepPolicy("messaging", connector="mail"),
        lambda _args: calls.append("sent"),
    )

    with pytest.raises(PermissionError, match="explicit connector capability"):
        orchestrator.execute_next(task.id)

    assert calls == []
    assert tasks.get(task.id).steps[0].status == "pending"
