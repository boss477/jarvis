"""Small in-memory registry for named agents used by the task orchestrator."""
from __future__ import annotations

from typing import Any


class AgentRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Any] = {}

    def register(self, name: str, agent: Any) -> Any:
        if not name:
            raise ValueError("agent name must not be empty")
        self._items[name] = agent
        return agent

    def get(self, name: str) -> Any | None:
        return self._items.get(name)

    def require(self, name: str) -> Any:
        try:
            return self._items[name]
        except KeyError as exc:
            raise LookupError(f"agent not registered: {name}") from exc

    def unregister(self, name: str) -> Any | None:
        return self._items.pop(name, None)

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)
