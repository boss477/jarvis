import errno
from datetime import datetime, timezone
from threading import Event, Thread
from pathlib import Path

import pytest

from friday.services import jsonl_store


def test_concurrent_appends_serialize_tail_repair(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    path.write_bytes(b'{"id":"kept"}\n{"partial":')

    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    second_attempting = Event()
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
        if record["id"] == "second":
            second_attempting.set()
        try:
            jsonl_store.append_jsonl_record(path, record)
        except Exception as exc:  # surface worker-thread failures in the test
            errors.append(exc)

    first = Thread(target=append, args=({"id": "first"},))
    second = Thread(target=append, args=({"id": "second"},))
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    assert second_attempting.wait(timeout=2)

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

def test_append_recovers_tail_before_escaping_lone_surrogates(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_bytes(b'{"id":"kept"}\n{"partial":')
    record = {"value": "\ud800"}

    jsonl_store.append_jsonl_record(path, record)

    assert list(jsonl_store.read_jsonl_records(path)) == [{"id": "kept"}, record]


class _FakeLockHandle:
    def __init__(self):
        self.seek_calls = 0

    def seek(self, offset, whence=0):
        self.seek_calls += 1

    def fileno(self):
        return 7


class _FakeMsvcrt:
    LK_NBLCK = 1

    def __init__(self, *, failures=0, error_number=errno.EACCES):
        self.failures_remaining = failures
        self.error_number = error_number
        self.calls = 0

    def locking(self, fd, mode, nbytes):
        self.calls += 1
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError(self.error_number, "lock unavailable")


def test_windows_lock_retries_contention_until_available(monkeypatch):
    monkeypatch.setattr(jsonl_store.time, "sleep", lambda _seconds: None)
    handle = _FakeLockHandle()
    msvcrt = _FakeMsvcrt(failures=2)

    jsonl_store._lock_windows_file(handle, msvcrt)

    assert msvcrt.calls == 3
    assert handle.seek_calls == 3


def test_windows_lock_times_out_when_contention_persists(monkeypatch):
    monkeypatch.setattr(jsonl_store, "_WINDOWS_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(jsonl_store.time, "sleep", lambda _seconds: None)
    msvcrt = _FakeMsvcrt(failures=3)

    with pytest.raises(TimeoutError, match="timed out waiting"):
        jsonl_store._lock_windows_file(_FakeLockHandle(), msvcrt)

    assert msvcrt.calls == 1


def test_windows_lock_propagates_non_contention_errors():
    msvcrt = _FakeMsvcrt(failures=1, error_number=errno.EBADF)

    with pytest.raises(OSError) as exc_info:
        jsonl_store._lock_windows_file(_FakeLockHandle(), msvcrt)

    assert exc_info.value.errno == errno.EBADF
    assert msvcrt.calls == 1


def test_append_to_terminated_file_does_not_read_the_entire_history(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    path.write_bytes(b"".join(
        (f'{{"id":"record-{index}"}}\n').encode("utf-8")
        for index in range(500)
    ))
    original_read_bytes = Path.read_bytes

    def reject_full_read(target):
        if target == path:
            raise AssertionError("append read the entire JSONL history")
        return original_read_bytes(target)

    monkeypatch.setattr(Path, "read_bytes", reject_full_read)
    jsonl_store.append_jsonl_record(path, {"id": "new"})
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
    assert path.read_text(encoding="utf-8").splitlines()[-1] == '{"id": "new"}'


def test_append_rejects_non_json_values_instead_of_stringifying(tmp_path):
    path = tmp_path / "history.jsonl"
    with pytest.raises(TypeError):
        jsonl_store.append_jsonl_record(
            path, {"created_at": datetime.now(timezone.utc)}
        )
    assert not path.exists()
