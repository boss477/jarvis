"""Durable task state for resumable multi-step Jarvis work.

This module is transport-agnostic: voice, Telegram, or an API can all create
and control the same task. Execution remains owned by the existing processors.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from .jsonl_store import append_jsonl_record, read_jsonl_records


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
            return {
                "id": task.id,
                "prompt": task.prompt,
                "status": task.status.value,
                "current_step": task.current_step,
                "total_steps": len(task.steps),
                "approval_reason": task.approval_reason,
                "next_step": task.steps[task.current_step].tool if task.current_step < len(task.steps) else None,
                "recovery_required": interrupted is not None,
                "error": failed_step.error if failed_step else "",
            }

    def recover_interrupted_tasks(self) -> list[str]:
        """Quarantine tasks left RUNNING by a previous host process.

        The step may have reached an external provider before the process died,
        so recovery never retries it automatically.
        """
        recovered = []
        uncertain = ("The host stopped while this step was executing; the external "
                     "outcome is uncertain. Verify the destination before creating a retry.")
        with self._locked_state():
            for task in self._tasks.values():
                if task.status != TaskStatus.RUNNING:
                    continue
                if task.current_step >= len(task.steps):
                    task.status = TaskStatus.COMPLETED
                else:
                    step = task.steps[task.current_step]
                    step.status = "interrupted"
                    step.error = uncertain
                    step.approved = False
                    task.status = TaskStatus.FAILED
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
        append_jsonl_record(self.path, row)

    def _load(self) -> None:
        self._tasks.clear()
        for row in read_jsonl_records(self.path):
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

            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = Path(f"{self.path}.mutex")
            with lock_path.open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                self._process_lock_depth.value = 1
                try:
                    if refresh:
                        self._load()
                    yield
                finally:
                    self._process_lock_depth.value = 0
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
