"""Strict JSONL reading with recovery for a process-crashed final append."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator

_thread_locks: dict[str, Any] = {}
_thread_locks_guard = threading.Lock()
_WINDOWS_LOCK_TIMEOUT_SECONDS = 60.0
_WINDOWS_LOCK_RETRY_INTERVAL_SECONDS = 0.05
_WINDOWS_LOCK_CONTENTION_ERRNOS = frozenset(
    {errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK}
)


def _lock_windows_file(handle: Any, msvcrt: Any) -> None:
    """Retry Windows sidecar-lock contention for a bounded period."""
    deadline = time.monotonic() + _WINDOWS_LOCK_TIMEOUT_SECONDS
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in _WINDOWS_LOCK_CONTENTION_ERRNOS:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for JSONL file lock") from exc
            time.sleep(min(_WINDOWS_LOCK_RETRY_INTERVAL_SECONDS, remaining))


@contextmanager
def _jsonl_file_lock(path: Path):
    """Use the same sidecar-lock convention as TaskManager for JSONL I/O."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{target}.mutex")
    key = str(lock_path.resolve())
    with _thread_locks_guard:
        local_lock = _thread_locks.setdefault(key, threading.RLock())

    with local_lock:
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                _lock_windows_file(handle, msvcrt)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_jsonl_records_unlocked(source: Path) -> Iterator[dict[str, Any]]:
    if not source.exists():
        return
    lines = source.read_bytes().splitlines(keepends=True)
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        is_final_partial = index == len(lines) - 1 and not raw.endswith((b"\n", b"\r"))
        try:
            line = raw.decode("utf-8")
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if is_final_partial:
                return
            raise ValueError(f"invalid JSONL record at {source}:{index + 1}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"expected JSON object at {source}:{index + 1}")
        yield record


def read_jsonl_records(
    path: str | Path, *, lock_held: bool = False
) -> Iterator[dict[str, Any]]:
    """Read records under the sidecar lock unless the caller already holds it."""
    source = Path(path)
    if lock_held:
        yield from _read_jsonl_records_unlocked(source)
        return
    with _jsonl_file_lock(source):
        yield from _read_jsonl_records_unlocked(source)


def _append_jsonl_record_unlocked(target: Path, record: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        data = target.read_bytes()
        if data and not data.endswith((b"\n", b"\r")):
            last_newline = max(data.rfind(b"\n"), data.rfind(b"\r"))
            offset = last_newline + 1
            tail = data[offset:]
            try:
                decoded = tail.decode("utf-8")
                parsed = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError):
                # A malformed non-terminated final record is the interrupted
                # append. Keep all preceding committed records intact.
                with target.open("r+b") as handle:
                    handle.truncate(offset)
            else:
                if not isinstance(parsed, dict):
                    with target.open("r+b") as handle:
                        handle.truncate(offset)
                else:
                    with target.open("ab") as handle:
                        handle.write(b"\n")
    encoded = json.dumps(record, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
    with target.open("ab") as handle:
        handle.write(encoded)


def append_jsonl_record(
    path: str | Path, record: dict[str, Any], *, lock_held: bool = False
) -> None:
    """Repair a truncated tail and append atomically with respect to other writers."""
    target = Path(path)
    if lock_held:
        _append_jsonl_record_unlocked(target, record)
        return
    with _jsonl_file_lock(target):
        _append_jsonl_record_unlocked(target, record)
