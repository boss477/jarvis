"""Small local-first memory store for facts and task outcomes."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .jsonl_store import _jsonl_file_lock, append_jsonl_record


def _file_signature(path: Path) -> tuple[int, int, int, int] | None:
    """Return a cheap change signature without creating files or directories."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


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
        # None forces one locked refresh on the first search if the file exists.
        # A missing file stays uncached without creating its parent or sidecar.
        self._file_signature = None

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
        with self._lock:
            signature = _file_signature(self.path)
            if signature is None:
                # A read against a fresh store must not create directories or
                # a .mutex file just to discover that there is no memory yet.
                self._items = []
                self._file_signature = None
            elif signature != self._file_signature:
                # The composer and host can write through separate instances.
                # Only lock and re-parse when the persisted file actually changed.
                with _jsonl_file_lock(self.path):
                    if _file_signature(self.path) is None:
                        self._items = []
                        self._file_signature = None
                    else:
                        self._items = _read_jsonl_records(self.path)
                        # Writers use the same sidecar lock, so this signature
                        # corresponds to the records just loaded.
                        self._file_signature = _file_signature(self.path)
            return [x for x in reversed(self._items)
                    if x.get("scope", "owner") == requested_scope
                    and all(term in x["text"].lower() for term in terms)][:limit]
