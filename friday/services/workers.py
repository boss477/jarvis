"""Capability contracts for Windows, browser, phone, and sandbox workers."""
from dataclasses import dataclass, field

@dataclass
class Worker:
    name: str
    capabilities: set[str] = field(default_factory=set)
    online: bool = False

class WorkerRegistry:
    def __init__(self): self._items: dict[str, Worker] = {}
    def register(self, name: str, capabilities: set[str]) -> Worker:
        worker = Worker(name, set(capabilities)); self._items[name] = worker; return worker
    def set_online(self, name: str, online: bool = True) -> Worker:
        worker = self._items[name]; worker.online = online; return worker
    def choose(self, capability: str) -> Worker:
        for worker in self._items.values():
            if worker.online and capability in worker.capabilities: return worker
        raise LookupError(f"no online worker for capability: {capability}")
