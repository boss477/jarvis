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



# Non-blocking locks are used by per-task execution locks. Their separate local
# lock registry lets a caller release the global registry lock when a task is
# busy, so unrelated tasks do not wait behind a long-running side effect.
_try_thread_locks: dict[str, Any] = {}
_try_thread_locks_guard = threading.Lock()


def _try_jsonl_file_lock(path: str | Path) -> tuple[Any, Any, bool] | None:
    """Try to acquire a sidecar lock without waiting; return its release handle."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{target}.mutex")
    key = str(lock_path.resolve())
    with _try_thread_locks_guard:
        local_lock = _try_thread_locks.setdefault(key, threading.Lock())
    if not local_lock.acquire(blocking=False):
        return None

    handle = None
    try:
        handle = lock_path.open("a+b")
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno not in _WINDOWS_LOCK_CONTENTION_ERRNOS:
                    raise
                handle.close()
                local_lock.release()
                return None
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in _WINDOWS_LOCK_CONTENTION_ERRNOS:
                    raise
                handle.close()
                local_lock.release()
                return None
        return handle, local_lock, os.name == "nt"
    except Exception:
        if handle is not None and not handle.closed:
            handle.close()
        local_lock.release()
        raise


def _release_try_jsonl_file_lock(lock: tuple[Any, Any, bool]) -> None:
    """Release a lock returned by _try_jsonl_file_lock."""
    handle, local_lock, is_windows = lock
    try:
        if is_windows:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        try:
            handle.close()
        finally:
            local_lock.release()


def _forget_try_jsonl_thread_lock(path: str | Path) -> None:
    """Forget an idle local lock after its sidecar has been safely removed."""
    key = str(Path(f"{Path(path)}.mutex").resolve())
    with _try_thread_locks_guard:
        local_lock = _try_thread_locks.get(key)
        if local_lock is None or not local_lock.acquire(blocking=False):
            return
        try:
            if _try_thread_locks.get(key) is local_lock:
                _try_thread_locks.pop(key, None)
        finally:
            local_lock.release()

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
        with target.open("r+b") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if end:
                handle.seek(-1, os.SEEK_END)
                last_byte = handle.read(1)
                if last_byte not in (b"\n", b"\r"):
                    offset = 0
                    position = end
                    while position > 0:
                        chunk_size = min(8192, position)
                        position -= chunk_size
                        handle.seek(position)
                        chunk = handle.read(chunk_size)
                        last_newline = max(chunk.rfind(b"\n"), chunk.rfind(b"\r"))
                        if last_newline >= 0:
                            offset = position + last_newline + 1
                            break
                    handle.seek(offset)
                    tail = handle.read()
                    try:
                        decoded = tail.decode("utf-8")
                        parsed = json.loads(decoded)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        # A malformed non-terminated final record is the interrupted
                        # append. Keep all preceding committed records intact.
                        handle.truncate(offset)
                    else:
                        if not isinstance(parsed, dict):
                            handle.truncate(offset)
                        else:
                            handle.seek(0, os.SEEK_END)
                            handle.write(b"\n")
    encoded = json.dumps(record, ensure_ascii=True, default=str).encode("utf-8") + b"\n"
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
