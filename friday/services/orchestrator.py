"""Policy-aware task step routing across registered workers and connectors."""
from __future__ import annotations
from dataclasses import dataclass
import threading
from typing import Any, Callable
from .agent_registry import AgentRegistry
from .connectors import ConnectorRegistry
from .task_manager import TaskManager, TaskStatus
from .workers import WorkerRegistry

@dataclass
class StepPolicy:
    capability: str
    connector: str | None = None
    connector_capability: str | None = None
    requires_approval: bool = False

class TaskOrchestrator:
    def __init__(self, tasks: TaskManager, workers: WorkerRegistry | None = None,
                 connectors: ConnectorRegistry | None = None, agents: AgentRegistry | None = None):
        self.tasks = tasks; self.workers = workers or WorkerRegistry()
        self.connectors = connectors or ConnectorRegistry(); self.agents = agents or AgentRegistry()
        self.policies: dict[str, StepPolicy] = {}
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._task_locks: dict[str, threading.Lock] = {}
        self._task_locks_guard = threading.Lock()

    def register_tool(self, tool: str, policy: StepPolicy, handler: Callable[[dict[str, Any]], Any]):
        self.policies[tool] = policy; self.handlers[tool] = handler

    def execute_next(self, task_id: str) -> dict[str, Any]:
        with self._task_locks_guard:
            task_lock = self._task_locks.setdefault(task_id, threading.Lock())
        with task_lock:
            return self._execute_next_locked(task_id)

    def _execute_next_locked(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None: raise KeyError(task_id)
        if task.status == TaskStatus.COMPLETED:
            return {"ok": True, "status": TaskStatus.COMPLETED.value}
        if task.status == TaskStatus.FAILED:
            return {"ok": False, "status": TaskStatus.FAILED.value,
                    "error": "failed tasks must not be replayed automatically"}
        if task.status in {TaskStatus.PAUSED, TaskStatus.CANCELLED, TaskStatus.WAITING_APPROVAL}:
            return {"ok": False, "status": task.status.value}
        if task.current_step >= len(task.steps): return {"ok": True, "status": "completed"}
        step = task.steps[task.current_step]; policy = self.policies.get(step.tool)
        if policy is None or step.tool not in self.handlers: raise LookupError(f"unregistered tool: {step.tool}")
        if policy.connector:
            if not policy.connector_capability:
                raise PermissionError(
                    f"tool {step.tool!r} names connector {policy.connector!r} "
                    "without an explicit connector capability"
                )
            self.connectors.require(policy.connector, policy.connector_capability)
        worker = self.workers.choose(policy.capability)
        if policy.requires_approval and not step.approved:
            self.tasks.require_approval(task_id, f"{step.tool} via {worker.name}")
            return {"ok": False, "status": "waiting_approval", "worker": worker.name}
        if not self.tasks.claim_step(task_id, task.current_step):
            current = self.tasks.get(task_id)
            return {"ok": False,
                    "status": current.status.value if current else TaskStatus.FAILED.value}
        try:
            result = self.handlers[step.tool](step.args)
            self.tasks.checkpoint(task_id, success=True, result=str(result))
            return {"ok": True, "status": self.tasks.get(task_id).status.value, "result": str(result)}
        except Exception as exc:
            self.tasks.checkpoint(task_id, success=False, error=str(exc))
            return {"ok": False, "status": "failed", "error": str(exc)}
