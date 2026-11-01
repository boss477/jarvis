"""Durable task state for resumable multi-step Jarvis work.

This module is transport-agnostic: voice, Telegram, or an API can all create
and control the same task. Execution remains owned by the existing processors.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import tempfile
import hashlib
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from .jsonl_store import (
    _forget_try_jsonl_thread_lock,
    _jsonl_file_lock,
    _release_try_jsonl_file_lock,
    _try_jsonl_file_lock,
    append_jsonl_record,
    read_jsonl_records,
)


_MAX_EXECUTION_LOCK_SIDECARS = 63  # plus one shared registry sidecar
_TASK_LOG_COMPACT_MIN_RECORDS = 256
_TASK_LOG_COMPACT_RECORDS_PER_TASK = 4


def _task_execution_lock_path(path: str | Path, task_id: str) -> Path:
    """Give each task a distinct, path-safe cross-process lock file."""
    digest = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()
    return Path(f"{Path(path).resolve()}.execution.{digest}")


def _task_execution_registry_path(path: str | Path) -> Path:
    """Serialize lock-file opening against cleanup without serializing task work."""
    return Path(f"{Path(path).resolve()}.execution.registry")


def _cleanup_task_execution_lock_sidecars(path: str | Path) -> None:
    """Prune idle per-task lock files while preventing open/unlink races.

    Every lock acquisition opens its task sidecar under the registry lock and
    uses a non-blocking OS lock. Cleanup holds that same registry lock, skips
    active sidecars, and removes only unlocked files, so waiters never retain
    handles to an inode that is being unlinked.
    """
    target = Path(path).resolve()
    registry_path = _task_execution_registry_path(target)
    prefix = f"{target.name}.execution."
    registry_sidecar = f"{target.name}.execution.registry.mutex"
    with _jsonl_file_lock(registry_path):
        sidecars = []
        for sidecar in target.parent.glob(f"{prefix}*.mutex"):
            if sidecar.name == registry_sidecar:
                continue
            suffix = sidecar.name[len(prefix):-len(".mutex")]
            if len(suffix) == 64 and all(char in "0123456789abcdef" for char in suffix):
                sidecars.append(sidecar)

        excess = len(sidecars) - _MAX_EXECUTION_LOCK_SIDECARS
        if excess <= 0:
            return

        def modified_ns(candidate: Path) -> int:
            try:
                return candidate.stat().st_mtime_ns
            except OSError:
                return 0

        for sidecar in sorted(sidecars, key=modified_ns):
            lock_target = Path(str(sidecar)[:-len(".mutex")])
            acquired = _try_jsonl_file_lock(lock_target)
            if acquired is None:
                continue
            try:
                _release_try_jsonl_file_lock(acquired)
            except OSError:
                continue
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                continue
            _forget_try_jsonl_thread_lock(lock_target)
            excess -= 1
            if excess <= 0:
                break


_task_execution_lock_state = threading.local()


@contextmanager
def task_execution_lock(path: str | Path, task_id: str):
    """Exclude recovery from one task while its side effect is in flight.

    The registry lock protects only sidecar open/cleanup. A per-task lock stays
    held across execution, while unrelated tasks use distinct lock files and
    can proceed concurrently. Re-entry by the same thread shares the outer
    lock, allowing a controller to call the orchestrator without deadlocking.
    Idle sidecars are pruned best-effort to a bounded count.
    """
    key = (str(Path(path).resolve()), str(task_id))
    held = getattr(_task_execution_lock_state, "held", None)
    if held is None:
        held = {}
        _task_execution_lock_state.held = held
    if key in held:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    execution_path = _task_execution_lock_path(path, task_id)
    registry_path = _task_execution_registry_path(path)
    while True:
        with _jsonl_file_lock(registry_path):
            acquired = _try_jsonl_file_lock(execution_path)
        if acquired is not None:
            break
        time.sleep(0.05)

    held[key] = 1
    try:
        yield
    finally:
        held.pop(key, None)
        try:
            _release_try_jsonl_file_lock(acquired)
        finally:
            try:
                _cleanup_task_execution_lock_sidecars(path)
            except (OSError, TimeoutError):
                # Pruning is maintenance only; it must not turn a completed
                # side effect into a failed task or mask the task's exception.
                pass

class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskStep:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    say: str = ""
    status: str = "pending"
    result: str = ""
    error: str = ""
    approved: bool = False


@dataclass
class Task:
    prompt: str
    steps: list[TaskStep]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: TaskStatus = TaskStatus.QUEUED
    current_step: int = 0
    approval_reason: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    origin_channel: str = ""
    origin_sender_id: str = ""
    origin_conversation_id: str = ""

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


class TaskManager:
    """Thread-safe task registry with append-only JSONL persistence."""

    def __init__(self, path: str | Path = "tasks.jsonl"):
        self.path = Path(path)
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()
        self._process_lock_depth = threading.local()
        self._record_count = 0
        with self._locked_state():
            pass

    def create(self, prompt: str, steps: list[dict[str, Any]], *,
               origin_channel: str = "", origin_sender_id: str = "",
               origin_conversation_id: str = "") -> Task:
        task = Task(prompt=prompt, steps=[TaskStep(**step) for step in steps],
                    origin_channel=origin_channel, origin_sender_id=origin_sender_id,
                    origin_conversation_id=origin_conversation_id)
        with self._locked_state():
            self._tasks[task.id] = task
            self._save(task)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._locked_state():
            return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        with self._locked_state():
            return list(self._tasks.values())

    def start(self, task_id: str) -> Task:
        return self._set_status(task_id, TaskStatus.RUNNING)

    def pause(self, task_id: str) -> Task:
        with self._locked_state():
            task = self._get_required(task_id)
            if task.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                raise ValueError("task is not queued or running")
            return self._set_status(task_id, TaskStatus.PAUSED)

    def resume(self, task_id: str) -> Task:
        with self._locked_state():
            task = self._get_required(task_id)
            if task.status not in {TaskStatus.PAUSED, TaskStatus.QUEUED}:
                raise ValueError("task is not paused or queued")
            return self._set_status(task_id, TaskStatus.RUNNING)

    def cancel(self, task_id: str) -> Task:
        return self._set_status(task_id, TaskStatus.CANCELLED)

    def require_approval(self, task_id: str, reason: str) -> Task:
        with self._locked_state():
            task = self._set_status(task_id, TaskStatus.WAITING_APPROVAL)
            task.approval_reason = reason
            self._persist(task)
            return task

    def approve(self, task_id: str) -> Task:
        with self._locked_state():
            task = self._get_required(task_id)
            if task.status != TaskStatus.WAITING_APPROVAL:
                raise ValueError("task is not waiting for approval")
            task.approval_reason = ""
            if task.current_step < len(task.steps):
                task.steps[task.current_step].approved = True
            task.status = TaskStatus.RUNNING
            self._persist(task)
            return task

    def deny(self, task_id: str) -> Task:
        with self._locked_state():
            task = self._get_required(task_id)
            if task.status != TaskStatus.WAITING_APPROVAL:
                raise ValueError("task is not waiting for approval")
            task.approval_reason = ""
            task.status = TaskStatus.CANCELLED
            self._persist(task)
            return task

    def claim_step(self, task_id: str, step_index: int) -> bool:
        """Durably claim a pending step only while its task remains runnable."""
        with self._locked_state():
            task = self._get_required(task_id)
            if task.status != TaskStatus.RUNNING or task.current_step != step_index:
                return False
            if step_index >= len(task.steps):
                return False
            step = task.steps[step_index]
            if step.status != "pending":
                return False
            step.status = "running"
            self._persist(task)
            return True

    def summary(self, task_id: str) -> dict[str, Any]:
        with self._locked_state():
            task = self._get_required(task_id)
            interrupted = next((step for step in task.steps if step.status == "interrupted"), None)
            failed_step = next((step for step in reversed(task.steps)
                                if step.status in {"failed", "interrupted"}), None)
            current_step = (task.steps[task.current_step]
                            if task.current_step < len(task.steps) else None)
            recovery_safe_to_resume = (
                task.status == TaskStatus.PAUSED
                and current_step is not None
                and current_step.status == "pending"
            )
            return {
                "id": task.id,
                "prompt": task.prompt,
                "status": task.status.value,
                "current_step": task.current_step,
                "total_steps": len(task.steps),
                "approval_reason": task.approval_reason,
                "next_step": current_step.tool if current_step else None,
                "recovery_required": interrupted is not None,
                "recovery_safe_to_resume": recovery_safe_to_resume,
                "error": failed_step.error if failed_step else "",
            }

    def recover_interrupted_tasks(self) -> list[str]:
        """Recover tasks left with an in-flight step after a process interruption.

        Each task's execution lock is acquired before refreshing and changing
        its state. This waits for live work on that task without blocking
        unrelated tasks or holding the JSONL lock while waiting.
        """
        recovered = []
        uncertain = ("The host stopped while this step was executing; the external "
                     "outcome is uncertain. Verify the destination before creating a retry.")
        with self._locked_state():
            task_ids = [
                task_id for task_id, task in self._tasks.items()
                if task.status in {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED}
            ]

        for task_id in task_ids:
            with task_execution_lock(self.path, task_id):
                with self._locked_state():
                    task = self._tasks.get(task_id)
                    if task is None:
                        continue
                    if task.status == TaskStatus.RUNNING:
                        if task.current_step >= len(task.steps):
                            task.status = TaskStatus.COMPLETED
                        else:
                            step = task.steps[task.current_step]
                            if step.status == "pending":
                                # The durable claim never happened, so no side effect started.
                                task.status = TaskStatus.PAUSED
                            else:
                                step.status = "interrupted"
                                step.error = uncertain
                                step.approved = False
                                task.status = TaskStatus.FAILED
                    elif task.status in {TaskStatus.PAUSED, TaskStatus.CANCELLED}:
                        if task.current_step >= len(task.steps):
                            continue
                        step = task.steps[task.current_step]
                        if step.status != "running":
                            continue
                        # Pause/cancel may race with an external call. Never make a
                        # possibly completed side effect runnable again after restart.
                        step.status = "interrupted"
                        step.error = uncertain
                        step.approved = False
                        if task.status == TaskStatus.PAUSED:
                            task.status = TaskStatus.FAILED
                        # Preserve CANCELLED as a terminal task state.
                    else:
                        continue
                    task.approval_reason = ""
                    self._persist(task)
                    recovered.append(task.id)
        return recovered

    def checkpoint(self, task_id: str, *, success: bool, result: str = "", error: str = "") -> Task:
        with self._locked_state():
            task = self._get_required(task_id)
            if task.status not in {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED}:
                raise ValueError(f"cannot checkpoint a task in {task.status.value} state")
            status_before_checkpoint = task.status
            if task.current_step >= len(task.steps):
                raise ValueError("task has no remaining steps")
            step = task.steps[task.current_step]
            step.status = "completed" if success else "failed"
            step.result, step.error = result, error
            if success:
                task.current_step += 1
                if task.current_step == len(task.steps):
                    task.status = TaskStatus.COMPLETED
                elif status_before_checkpoint in {TaskStatus.PAUSED, TaskStatus.CANCELLED}:
                    task.status = status_before_checkpoint
            else:
                task.status = (TaskStatus.CANCELLED
                               if status_before_checkpoint == TaskStatus.CANCELLED
                               else TaskStatus.FAILED)
            self._persist(task)
            return task

    def _set_status(self, task_id: str, status: TaskStatus) -> Task:
        with self._locked_state():
            task = self._get_required(task_id)
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                raise ValueError(f"cannot change terminal task status {task.status.value}")
            task.status = status
            self._persist(task)
            return task

    def _get_required(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        return task

    def _persist(self, task: Task) -> None:
        with self._locked_state(refresh=False):
            task.touch()
            self._save(task)

    def _save(self, task: Task) -> None:
        row = asdict(task)
        row["status"] = task.status.value
        append_jsonl_record(self.path, row, lock_held=True)
        self._record_count += 1
        self._compact_if_needed()

    def _compact_if_needed(self) -> None:
        """Atomically replace old task events with one latest snapshot per task.

        Task state is always refreshed while holding the JSONL sidecar lock, so
        compaction can safely preserve the current cross-process state. Keeping
        a bounded number of historical rows avoids rereading an ever-growing
        append-only event log on every operation.
        """
        threshold = max(
            _TASK_LOG_COMPACT_MIN_RECORDS,
            len(self._tasks) * _TASK_LOG_COMPACT_RECORDS_PER_TASK,
        )
        if self._record_count < threshold:
            return

        temporary_path = None
        descriptor = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\\n") as handle:
                descriptor = None
                for current in self._tasks.values():
                    snapshot = asdict(current)
                    snapshot["status"] = current.status.value
                    handle.write(json.dumps(snapshot, ensure_ascii=True) + "\\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            self._record_count = len(self._tasks)
        except OSError:
            # Compaction is maintenance: the already-appended task update is
            # durable, so a platform/file-lock failure must not fail the task.
            return
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load(self) -> None:
        self._tasks.clear()
        self._record_count = 0
        for row in read_jsonl_records(self.path, lock_held=True):
            self._record_count += 1
            row["status"] = TaskStatus(row["status"])
            row["steps"] = [TaskStep(**step) for step in row["steps"]]
            task = Task(**row)
            self._tasks[task.id] = task

    @contextmanager
    def _locked_state(self, *, refresh: bool = True):
        """Serialize state access across manager instances and processes.

        The JSONL log remains authoritative; refreshing under the OS lock keeps
        long-lived host and composer processes from making decisions on stale
        task snapshots. Nested manager operations reuse their outer lock.
        """
        with self._lock:
            depth = getattr(self._process_lock_depth, "value", 0)
            if depth:
                self._process_lock_depth.value = depth + 1
                try:
                    yield
                finally:
                    self._process_lock_depth.value -= 1
                return

            with _jsonl_file_lock(self.path):
                self._process_lock_depth.value = 1
                try:
                    if refresh:
                        self._load()
                    yield
                finally:
                    self._process_lock_depth.value = 0
