"""Construction helpers for the Windows persistent action agent."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .agent_controller import TaskAgentController
from .agent_tools import register_agent_tools
from .communication_hub import CommunicationHub, build_default_hub
from .orchestrator import TaskOrchestrator
from .persistent_agent import AgentRuntimeConfig, PersistentAgentRuntime
from .remote_gateway import RemoteGateway
from .task_commands import TaskCommandService
from .task_manager import TaskManager
from .memory_store import MemoryStore
from .workers import WorkerRegistry


def build_persistent_agent(
    planner: Callable[[str], list[dict[str, Any]]],
    *,
    state_path: str | Path = "agent-state/tasks.jsonl",
    hub: CommunicationHub | None = None,
    workers: WorkerRegistry | None = None,
    config: AgentRuntimeConfig | None = None,
) -> PersistentAgentRuntime:
    """Build the complete agent runtime with durable local task state.

    Provider credentials are read by the channel adapters from environment
    variables. The planner is injected so the same runtime works with a local
    LLM, a remote LLM, or a deterministic test planner.
    """
    tasks = TaskManager(state_path)
    worker_registry = workers or WorkerRegistry()
    if "communication" not in worker_registry._items:
        worker_registry.register("communication", {"communication"})
    worker_registry.set_online("communication")
    orchestrator = TaskOrchestrator(tasks, worker_registry)
    communication_hub = hub or build_default_hub()
    memory = MemoryStore(Path(state_path).with_name("memory.jsonl"))
    register_agent_tools(orchestrator, communication_hub, worker_registry, memory)
    controller = TaskAgentController(tasks, orchestrator, planner)
    runtime = PersistentAgentRuntime(
        communication_hub, controller.handle, config,
        history_path=Path(state_path).with_name("conversations.jsonl"),
    )
    runtime.tasks = tasks
    runtime.memory = memory
    runtime.orchestrator = orchestrator
    runtime.controller = controller
    controller.history_provider = lambda message: [
        ("Agent: " if item.sender_id == "agent" else f"{item.sender_id}: ") + item.text
        for item in runtime.agent.recent_history(
            message.channel, message.conversation_id, limit=12,
            exclude_message_id=message.message_id,
        )
    ]
    def handle_webhook_message(message):
        from .communication_hub import CommunicationMessage
        result = runtime.agent.process_message(CommunicationMessage(
            channel=message.channel, conversation_id=message.conversation_id or message.sender_id,
            sender_id=message.sender_id, text=message.text, message_id=message.message_id,
            metadata=message.metadata,
        ), deliver=False)
        return result.reply

    runtime.webhook_gateway = RemoteGateway(
        TaskCommandService(tasks), message_handler=handle_webhook_message,
        resume_handler=controller.advance,
        delivery_handler=runtime.agent.record_delivery,
        delivery_claim_handler=runtime.agent.claim_delivery,
    )
    return runtime
