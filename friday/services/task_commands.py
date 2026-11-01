"""Small transport-neutral command surface for Telegram/WhatsApp/API adapters."""
from __future__ import annotations
from .task_manager import TaskManager, TaskStatus


class TaskCommandService:
    def __init__(self, manager: TaskManager):
        self.manager = manager

    def check(self, task_id: str) -> dict:
        return self.manager.summary(task_id)

    def pause(self, task_id: str) -> dict:
        return self.manager.summary(self.manager.pause(task_id).id)

    def resume(self, task_id: str) -> dict:
        return self.manager.summary(self.manager.resume(task_id).id)

    def cancel(self, task_id: str) -> dict:
        return self.manager.summary(self.manager.cancel(task_id).id)

    def approve(self, task_id: str) -> dict:
        return self.manager.summary(self.manager.approve(task_id).id)

    def deny(self, task_id: str) -> dict:
        return self.manager.summary(self.manager.deny(task_id).id)

    def pending(self, channel: str, sender_id: str, conversation_id: str) -> list[dict]:
        """List only approval-waiting tasks owned by this exact conversation."""
        tasks = [task for task in self.manager.list()
                 if task.status == TaskStatus.WAITING_APPROVAL
                 and task.origin_channel == channel
                 and task.origin_sender_id == sender_id
                 and task.origin_conversation_id == conversation_id]
        tasks.sort(key=lambda task: task.created_at)
        return [self.manager.summary(task.id) for task in tasks]

    def recovery_required(self, channel: str, sender_id: str,
                          conversation_id: str) -> list[dict]:
        """List uncertain tasks and safely resumable unclaimed steps for this conversation."""
        tasks = [task for task in self.manager.list()
                 if task.origin_channel == channel
                 and task.origin_sender_id == sender_id
                 and task.origin_conversation_id == conversation_id
                 and (
                     any(step.status == "interrupted" for step in task.steps)
                     or (
                         task.status == TaskStatus.PAUSED
                         and task.current_step < len(task.steps)
                         and task.steps[task.current_step].status == "pending"
                     )
                 )]
        tasks.sort(key=lambda task: task.updated_at, reverse=True)
        return [self.manager.summary(task.id) for task in tasks]
