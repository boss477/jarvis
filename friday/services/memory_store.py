"""Small local-first memory store for facts and task outcomes."""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path

class MemoryStore:
    def __init__(self, path: str | Path = "memory.jsonl"):
        self.path = Path(path); self._lock = threading.Lock(); self._items = []
        if self.path.exists():
            self._items = [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def remember(self, text: str, *, kind: str = "fact", source: str = "user",
                 scope: str = "owner") -> dict:
        item = {"text": text.strip(), "kind": kind, "source": source,
                "scope": scope.strip() or "owner", "created_at": time.time()}
        if not item["text"]: raise ValueError("memory text is required")
        with self._lock:
            self._items.append(item); self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f: f.write(json.dumps(item) + "\n")
        return item

    def search(self, query: str, limit: int = 10, *, scope: str = "owner") -> list[dict]:
        terms = query.lower().split()
        if not terms:
            raise ValueError("memory search query is required")
        requested_scope = scope.strip() or "owner"
        return [x for x in reversed(self._items)
                if x.get("scope", "owner") == requested_scope
                and all(term in x["text"].lower() for term in terms)][:limit]
