"""Capability-aware registry for external connectors."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Connector:
    name: str
    capabilities: set[str] = field(default_factory=set)
    client: Any = None
    enabled: bool = True


class ConnectorRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Connector] = {}

    def register(self, name: str, capabilities: set[str], client: Any = None) -> Connector:
        if not name:
            raise ValueError("connector name must not be empty")
        connector = Connector(name=name, capabilities=set(capabilities), client=client)
        self._items[name] = connector
        return connector

    def get(self, name: str) -> Connector | None:
        return self._items.get(name)

    def require(self, name: str, capability: str | None = None) -> Connector:
        connector = self._items.get(name)
        if connector is None:
            raise LookupError(f"connector not registered: {name}")
        if not connector.enabled:
            raise LookupError(f"connector is disabled: {name}")
        # Callers must name the operation being authorized. Generic use is
        # available only when the caller explicitly requests it.
        if not capability:
            raise ValueError("connector capability must be explicit")
        if capability != "use" and capability not in connector.capabilities:
            raise PermissionError(f"connector {name!r} does not grant capability {capability!r}")
        return connector

    def unregister(self, name: str) -> Connector | None:
        return self._items.pop(name, None)
