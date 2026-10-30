"""Small local-first memory store for facts and task outcomes."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .jsonl_store import append_jsonl_record


def _read_jsonl_records(path: Path) -> list[dict]:
    """Read complete JSONL records, tolerating only a truncated final append."""
    if not path.exists():
        return []

    records = []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            is_final_line = index == len(lines) - 1
            has_line_terminator = raw_line.endswith(("\n", "\r"))
            if is_final_line and not has_line_terminator:
                break
            raise
    return records


class MemoryStore:
    def __init__(self, path: str | Path = "memory.jsonl"):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._items = _read_jsonl_records(self.path)

    def remember(self, text: str, *, kind: str = "fact", source: str = "user",
                 scope: str = "owner") -> dict:
        item = {"text": text.strip(), "kind": kind, "source": source,
                "scope": scope.strip() or "owner", "created_at": time.time()}
        if not item["text"]:
            raise ValueError("memory text is required")
        with self._lock:
            append_jsonl_record(self.path, item)
            self._items.append(item)
        return item

    def search(self, query: str, limit: int = 10, *, scope: str = "owner") -> list[dict]:
        terms = query.lower().split()
        if not terms:
            raise ValueError("memory search query is required")
        requested_scope = scope.strip() or "owner"
        return [x for x in reversed(self._items)
                if x.get("scope", "owner") == requested_scope
                and all(term in x["text"].lower() for term in terms)][:limit]
