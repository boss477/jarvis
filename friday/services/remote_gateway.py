"""Remote-message contract shared by Telegram, WhatsApp, and future channels."""
from __future__ import annotations
from dataclasses import dataclass
from dataclasses import field
import logging
import re
from typing import Any
from .task_commands import TaskCommandService

logger = logging.getLogger(__name__)

@dataclass
class IncomingMessage:
    channel: str
    sender_id: str
    text: str
    message_id: str = ""
    conversation_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

class RemoteGateway:
    def __init__(self, tasks: TaskCommandService, message_handler=None, resume_handler=None,
                 delivery_handler=None, delivery_claim_handler=None):
        self.tasks = tasks
        self.message_handler = message_handler
        self.resume_handler = resume_handler
        self.delivery_handler = delivery_handler
        self.delivery_claim_handler = delivery_claim_handler

    def route_command(self, message: IncomingMessage) -> dict:
        inbound_text = message.text
        if message.channel == "whatsapp":
            button = re.fullmatch(
                r"task:(approve|deny):([0-9a-f]{12})",
                str((message.metadata or {}).get("interactive_reply_id", "")),
            )
            if button:
                inbound_text = f"/{button.group(1)} {button.group(2)}"
        parts = inbound_text.strip().split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        task_id = parts[1].strip() if len(parts) > 1 else ""
        affirmative_followup = bool(re.fullmatch(
            r"(?:yes|yeah|yep|y|ok|okay|sure)(?:[\s,!.]+(?:please|do it|go ahead|proceed))?[.! ]*",
            inbound_text.strip(), re.IGNORECASE,
        ))
        if message.channel == "whatsapp" and affirmative_followup:
            conversation_id = message.conversation_id or message.sender_id
            pending = self.tasks.pending(
                message.channel, message.sender_id, conversation_id,
            )
            if len(pending) == 1:
                pending_id = pending[0]["id"]
                result = {
                    "ok": True,
                    "reply": (f"Task {pending_id} is still waiting for approval. "
                              "Review the exact action in the earlier message, then "
                              f"tap Approve or send /approve {pending_id}."),
                }
                self._attach_approval_buttons(result, message, pending_id)
                return result
            if len(pending) > 1:
                choices = "\n".join(
                    f"- {item['id']}: {str(item['prompt']).replace(chr(10), ' ')[:100]}"
                    for item in pending
                )
                return {
                    "ok": True,
                    "reply": ("There are multiple tasks waiting for approval. "
                              "Choose the intended task with /approve <task-id> after "
                              "reviewing its approval prompt:\n" + choices),
                }
        if command == "/help":
            if task_id:
                return {"ok": False, "reply": "Usage: /help"}
            return {"ok": True, "reply": (
                "Commands: /pending, /check <id>, /approve <id>, /deny <id>, "
                "/pause <id>, /resume <id>, /cancel <id>. "
                "Use /pending to see your tasks waiting for approval or review."
            )}
        if command == "/pending":
            if task_id:
                return {"ok": False, "reply": "Usage: /pending"}
            conversation_id = message.conversation_id or message.sender_id
            tasks = self.tasks.pending(
                message.channel, message.sender_id,
                conversation_id,
            )
            recovery = self.tasks.recovery_required(
                message.channel, message.sender_id, conversation_id,
            )
            if not tasks and not recovery:
                return {"ok": True, "reply": "You have no tasks waiting for approval or recovery."}
            lines = []
            if tasks:
                lines.append("Tasks waiting for your approval:")
            for task in tasks:
                prompt = str(task["prompt"]).replace("\n", " ")[:120]
                lines.append(f"- {task['id']}: {prompt} (next: {task['next_step']})")
            if tasks:
                lines.append("Reply /approve <task-id> to proceed or /deny <task-id> to cancel.")
            if recovery:
                lines.append("Tasks interrupted during execution (outcome uncertain):")
                for task in recovery:
                    prompt = str(task["prompt"]).replace("\n", " ")[:120]
                    lines.append(f"- {task['id']}: {prompt}")
                lines.append(
                    "Check each destination before retrying; the interrupted action was not "
                    "replayed automatically. Use /check <task-id> for details."
                )
            return {"ok": True, "reply": "\n".join(lines)}
        handlers = {"/check": self.tasks.check, "/pause": self.tasks.pause,
                    "/resume": self.tasks.resume, "/cancel": self.tasks.cancel,
                    "/approve": self.tasks.approve, "/deny": self.tasks.deny}
        if command in handlers and not task_id:
            return {"ok": False, "reply": f"Usage: {command} <task-id>"}
        if command.startswith("/") and command not in handlers:
            return {"ok": False, "reply": "Unknown command. Send /help for available commands."}
        if command not in handlers:
            if self.message_handler is None:
                return {"ok": False, "reply": "Use /pending, /check, /pause, /resume, /cancel, /approve, or /deny with a task ID where required."}
            try:
                reply = self.message_handler(message)
                result = {"ok": True, "reply": reply}
                approval_match = re.match(
                    r"Task ([0-9a-f]{12}) needs approval\.", str(reply)
                )
                if approval_match:
                    self._attach_approval_buttons(
                        result, message, approval_match.group(1),
                        require_matching_prompt=True,
                    )
                return result
            except Exception as exc:
                logger.warning("inbound message handling failed (%s)", type(exc).__name__)
                return {"ok": False, "retry": True,
                        "reply": "Temporary processing issue. The message can be retried."}
        manager = getattr(self.tasks, "manager", None)
        task_record = manager.get(task_id) if manager is not None else None
        if task_record is not None:
            origin = (task_record.origin_channel, task_record.origin_sender_id,
                      task_record.origin_conversation_id)
            requester = (message.channel, message.sender_id,
                         message.conversation_id or message.sender_id)
            if not all(origin) or requester != origin:
                return {"ok": False,
                        "reply": "Task controls are limited to the channel, sender, and conversation that created the task."}
        try:
            task = handlers[command](task_id)
            if command == "/check":
                lines = [f"Task {task['id']}: {task['status']}"]
                if task.get("recovery_required"):
                    lines.append("The interrupted step's outcome is uncertain. "
                                 "Verify the destination before creating a retry.")
                elif task.get("error"):
                    lines.append("Last step error: " + str(task["error"]))
                return {"ok": True, "task": task, "reply": "\n".join(lines)}
            if command in {"/approve", "/resume"} and self.resume_handler:
                result = {"ok": True, "task": task,
                          "reply": self.resume_handler(task_id)}
                self._attach_approval_buttons(result, message, task_id)
                return result
            result = {"ok": True, "task": task}
            return result
        except (KeyError, ValueError) as exc: return {"ok": False, "reply": str(exc)}

    def _attach_approval_buttons(self, result: dict, message: IncomingMessage,
                                 task_id: str, *, require_matching_prompt: bool = False) -> None:
        if message.channel != "whatsapp":
            return
        manager = getattr(self.tasks, "manager", None)
        task = manager.get(task_id) if manager is not None else None
        if task is None or task.status.value != "waiting_approval" or not task.approval_reason:
            return
        owner = (task.origin_channel, task.origin_sender_id, task.origin_conversation_id)
        requester = (message.channel, message.sender_id,
                     message.conversation_id or message.sender_id)
        if owner != requester:
            return
        if require_matching_prompt and task.prompt != message.text:
            return
        result["actions"] = [
            {"id": f"task:approve:{task.id}", "title": "Approve"},
            {"id": f"task:deny:{task.id}", "title": "Deny"},
        ]

    def handle_payload(self, adapter, payload: dict) -> list[dict]:
        """Parse a provider webhook and route each command message."""
        return [self.route_command(message) for message in adapter.parse(payload)]

class UnconfiguredChannel:
    """Explicit placeholder until a real provider credential is configured."""
    def __init__(self, name: str): self.name = name
    def send(self, recipient: str, text: str) -> str:
        return f"{self.name} is not configured; message not sent."
