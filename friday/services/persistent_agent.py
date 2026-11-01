"""Background runtime for a cross-channel action agent."""
from __future__ import annotations

import threading
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .agent_controller import TaskAgentController
    from .memory_store import MemoryStore
    from .orchestrator import TaskOrchestrator
    from .remote_gateway import RemoteGateway
    from .task_manager import TaskManager

from .action_agent import AgentReply, PersistentActionAgent
from .communication_hub import CommunicationHub, CommunicationMessage

logger = logging.getLogger(__name__)


@dataclass
class AgentRuntimeConfig:
    poll_interval_seconds: float = 2.0
    max_consecutive_errors: int = 5
    max_backoff_seconds: float = 60.0


class PersistentAgentRuntime:
    """Owns the lifecycle of a continuously running communication agent."""

    # These services are attached by build_persistent_agent after construction.
    # Declare them here so the factory-built runtime has a typed public surface.
    tasks: TaskManager
    memory: MemoryStore
    orchestrator: TaskOrchestrator
    controller: TaskAgentController
    webhook_gateway: RemoteGateway

    def __init__(self, hub: CommunicationHub,
                 handler: Callable[[CommunicationMessage], str],
                 config: AgentRuntimeConfig | None = None,
                 on_replies: Callable[[list[AgentReply]], None] | None = None,
                 history_path: str | Path | None = None):
        self.config = config or AgentRuntimeConfig()
        self.agent = PersistentActionAgent(hub, handler, history_path)
        self.on_replies = on_replies
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._errors = 0
        self.last_error: str = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def consecutive_errors(self) -> int:
        return self._errors

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="persistent-agent", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        # Keep the reference while the worker is still alive. Otherwise start()
        # could replace it and launch a second worker after a timed-out join.
        if not thread.is_alive() and self._thread is thread:
            self._thread = None

    def process_once(self) -> list[AgentReply]:
        replies = self.agent.process_once()
        if replies and self.on_replies:
            self.on_replies(replies)
        return replies

    def _run(self) -> None:
        while not self._stop.is_set():
            wait_seconds = self.config.poll_interval_seconds
            try:
                self.process_once()
                self._errors = 0
                self.last_error = ""
            except Exception as exc:  # keep the companion alive through provider outages
                self._errors += 1
                self.last_error = str(exc)
                logger.warning("agent worker cycle failed (%s: %s); consecutive failures=%d",
                               type(exc).__name__, exc, self._errors)
                if self._errors >= self.config.max_consecutive_errors:
                    exponent = self._errors - self.config.max_consecutive_errors + 1
                    wait_seconds = min(
                        self.config.poll_interval_seconds * (2 ** min(exponent, 10)),
                        self.config.max_backoff_seconds,
                    )
            self._stop.wait(max(0.0, wait_seconds))
