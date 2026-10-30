from threading import Event, Thread

from friday.services import jsonl_store


def test_concurrent_appends_serialize_tail_repair(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    path.write_bytes(b'{"id":"kept"}\n{"partial":')

    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    errors = []
    original_append = jsonl_store._append_jsonl_record_unlocked

    def controlled_append(target, record):
        if record["id"] == "first":
            first_entered.set()
            if not release_first.wait(timeout=3):
                raise AssertionError("test did not release the first append")
        elif record["id"] == "second":
            second_entered.set()
        original_append(target, record)

    monkeypatch.setattr(jsonl_store, "_append_jsonl_record_unlocked", controlled_append)

    def append(record):
        try:
            jsonl_store.append_jsonl_record(path, record)
        except Exception as exc:  # surface worker-thread failures in the test
            errors.append(exc)

    first = Thread(target=append, args=({"id": "first"},))
    second = Thread(target=append, args=({"id": "second"},))
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()

    # The second writer must not enter tail repair while the first owns the lock.
    assert not second_entered.wait(timeout=0.15)
    release_first.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert [record["id"] for record in jsonl_store.read_jsonl_records(path)] == [
        "kept",
        "first",
        "second",
    ]
