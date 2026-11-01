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


def test_search_reloads_records_written_by_another_store(tmp_path):
    path = tmp_path / "memory.jsonl"
    host_store = MemoryStore(path)
    composer_store = MemoryStore(path)

    added = composer_store.remember("fact saved by the composer")

    assert host_store.search("fact saved by the composer") == [added]



def test_search_missing_file_does_not_create_parent_or_lock_file(tmp_path):
    path = tmp_path / "not-created-yet" / "memory.jsonl"
    store = MemoryStore(path)

    assert store.search("anything") == []
    assert not path.parent.exists()
    assert not Path(f"{path}.mutex").exists()


def test_search_does_not_reparse_unchanged_file(tmp_path, monkeypatch):
    import friday.services.memory_store as memory_store

    path = tmp_path / "memory.jsonl"
    item = {
        "text": "cached fact",
        "kind": "fact",
        "source": "user",
        "scope": "owner",
        "created_at": 1,
    }
    path.write_text(json.dumps(item) + "\n", encoding="utf-8")
    store = MemoryStore(path)

    original_read = memory_store._read_jsonl_records
    calls = 0

    def counted_read(source):
        nonlocal calls
        calls += 1
        return original_read(source)

    monkeypatch.setattr(memory_store, "_read_jsonl_records", counted_read)

    assert store.search("cached fact") == [item]
    assert calls == 1
    assert store.search("cached fact") == [item]
    assert calls == 1
