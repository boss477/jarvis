import json
from pathlib import Path

import pytest

from friday.services.memory_store import MemoryStore


def test_recovers_truncated_final_jsonl_record(tmp_path):
    path = tmp_path / "memory.jsonl"
    valid = {
        "text": "keep this",
        "kind": "fact",
        "source": "user",
        "scope": "owner",
        "created_at": 1,
    }
    path.write_text(
        json.dumps(valid) + "\n" + '{"text":"interrupted',
        encoding="utf-8",
    )

    store = MemoryStore(path)

    assert store.search("keep this") == [valid]
    assert store.search("interrupted") == []


def test_remember_repairs_truncated_tail_before_appending(tmp_path):
    path = tmp_path / "memory.jsonl"
    valid = {
        "text": "keep this",
        "kind": "fact",
        "source": "user",
        "scope": "owner",
        "created_at": 1,
    }
    path.write_text(
        json.dumps(valid) + "\n" + '{"text":"interrupted',
        encoding="utf-8",
    )

    store = MemoryStore(path)
    added = store.remember("new memory")

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert records == [valid, added]
    assert store.search("keep this") == [valid]
    assert store.search("new memory") == [added]


def test_does_not_ignore_corrupt_complete_record(tmp_path):
    path = tmp_path / "memory.jsonl"
    path.write_text('{"text":"good"}\n{"broken":\n', encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        MemoryStore(path)


def test_failed_persistence_does_not_add_to_memory(tmp_path, monkeypatch):
    path = tmp_path / "memory.jsonl"
    store = MemoryStore(path)
    original_open = Path.open

    class FailingWriter:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, _content):
            raise OSError("disk full")

    def failing_open(self, *args, **kwargs):
        if self == path and args and args[0] in ("a", "ab"):
            return FailingWriter()
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError, match="disk full"):
        store.remember("not persisted")

    assert store.search("not persisted") == []
