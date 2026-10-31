"""Small Windows channel-composition window for the persistent action agent."""
from __future__ import annotations

import tkinter as tk
import json
import os
from pathlib import Path
from tkinter import messagebox, ttk

from dotenv import load_dotenv
from dotenv import dotenv_values

from .agent_controller import approval_text_limit
from .agent_planner import plan_with_openai_compatible_model
from .agent_factory import build_persistent_agent
from .remote_http import whatsapp_sender_allowlist_configured


def _configured_state_path() -> Path:
    """Use the same durable task-store path as the persistent host."""
    path = Path(os.getenv("AGENT_TASKS_PATH", "agent-state/tasks.jsonl"))
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path.resolve()


class ComposeWindow:
    def __init__(self, runtime=None):
        self.env_path = Path(__file__).resolve().parents[1] / ".env"
        self._external_environment_keys = set(os.environ)
        load_dotenv(self.env_path)
        # Track keys provided by .env so Refresh setup can remove values that
        # were deleted from the file without touching shell-provided settings.
        self._dotenv_keys = set(dotenv_values(self.env_path))
        self.runtime = runtime or build_persistent_agent(
            plan_with_openai_compatible_model, state_path=_configured_state_path()
        )
        self.root = tk.Tk()
        self.root.title("Jarvis · Compose")
        self.root.geometry("760x820")
        self.root.minsize(620, 680)
        self.root.configure(bg="#111827")
        self._build()

    def _build(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#111827")
        style.configure("TLabel", background="#111827", foreground="#e5e7eb")
        style.configure("Header.TLabel", font=("Segoe UI", 19, "bold"), foreground="#f9fafb")
        style.configure("Hint.TLabel", foreground="#9ca3af")
        style.configure("TButton", padding=8)

        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Compose across channels", style="Header.TLabel").pack(anchor="w")
        ttk.Label(frame, text="One message, routed to the selected destinations.",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 18))
        self.readiness = tk.StringVar(value=self._readiness_summary())
        ttk.Label(frame, textvariable=self.readiness, style="Hint.TLabel", wraplength=600,
                  justify="left").pack(anchor="w", pady=(0, 12))

        ttk.Label(frame, text="Message").pack(anchor="w")
        self.message = tk.Text(frame, height=7, wrap="word", bg="#1f2937", fg="#f9fafb",
                               insertbackground="white", relief="flat", padx=10, pady=9)
        self.message.pack(fill="x", pady=(6, 16))

        ttk.Label(frame, text="Destinations").pack(anchor="w")
        ttk.Label(frame, text="Choose a channel, enter its recipient, then add it. Public posts and calls require approval.",
                  style="Hint.TLabel", wraplength=680).pack(anchor="w", pady=(3, 6))
        destination_row = ttk.Frame(frame)
        destination_row.pack(fill="x", pady=(0, 6))
        ttk.Label(destination_row, text="Channel").grid(row=0, column=0, sticky="w")
        ttk.Label(destination_row, text="Recipient / channel ID / public").grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        self.destination_channel = tk.StringVar()
        self.channel_picker = ttk.Combobox(
            destination_row, textvariable=self.destination_channel,
            values=self.runtime.agent.hub.channels(), state="readonly", width=23,
        )
        self.channel_picker.grid(row=1, column=0, sticky="ew")
        channel_values = self.channel_picker["values"]
        if channel_values:
            self.destination_channel.set(channel_values[0])
        self.destination_recipient = tk.StringVar()
        self.recipient_entry = ttk.Entry(destination_row, textvariable=self.destination_recipient)
        self.recipient_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(destination_row, text="Add", command=self._add_destination).grid(
            row=1, column=2, padx=(8, 0))
        destination_row.columnconfigure(0, weight=1)
        destination_row.columnconfigure(1, weight=2)
        self.destination_list = ttk.Treeview(
            frame, columns=("channel", "recipient"), show="headings", height=4,
            selectmode="extended",
        )
        self.destination_list.heading("channel", text="Channel")
        self.destination_list.heading("recipient", text="Recipient")
        self.destination_list.column("channel", width=170, stretch=False)
        self.destination_list.column("recipient", width=480, stretch=True)
        self.destination_list.pack(fill="x", pady=(0, 4))
        ttk.Button(frame, text="Remove selected", command=self._remove_destination).pack(
            anchor="e", pady=(0, 12))
        self.selected_targets: list[dict[str, str]] = []

        approval_row = ttk.Frame(frame)
        approval_row.pack(fill="x", pady=(0, 12))
        ttk.Label(approval_row, text="Saved compose tasks").pack(side="left", padx=(0, 8))
        self.pending_task = tk.StringVar()
        self.pending_picker = ttk.Combobox(approval_row, textvariable=self.pending_task,
                                           state="readonly", width=48)
        self.pending_picker.pack(side="left", fill="x", expand=True)
        self.pending_picker.bind("<<ComboboxSelected>>", self._show_selected_task)
        ttk.Button(approval_row, text="Refresh", command=self._refresh_pending).pack(
            side="left", padx=(8, 0))

        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        ttk.Button(controls, text="Open WhatsApp Web", command=self._open_whatsapp).pack(side="left")
        ttk.Button(controls, text="Open provider settings", command=self._open_provider_settings).pack(
            side="left", padx=(8, 0))
        ttk.Button(controls, text="Refresh setup", command=self._refresh_setup).pack(
            side="left", padx=(8, 0))
        self.send_button = ttk.Button(controls, text="Prepare for approval", command=self._prepare)
        self.send_button.pack(side="right")
        ttk.Label(frame, text="Nothing is sent until you approve the task.",
                  style="Hint.TLabel").pack(anchor="w", pady=(8, 0))

        ttk.Label(frame, text="Task status", padding=(0, 18, 0, 5)).pack(anchor="w")
        self.status = tk.StringVar(value="Ready")
        ttk.Label(frame, textvariable=self.status, style="Hint.TLabel", wraplength=600,
                  justify="left").pack(anchor="w")
        self.preview = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.preview, style="Hint.TLabel", wraplength=600,
                  justify="left").pack(anchor="w", pady=(6, 0))
        actions = ttk.Frame(frame)
        actions.pack(anchor="w", pady=(10, 0))
        ttk.Button(actions, text="Approve & send", command=self._approve).pack(side="left")
        ttk.Button(actions, text="Cancel task", command=self._cancel).pack(side="left", padx=8)
        self.retry_button = ttk.Button(actions, text="Retry failed destinations",
                                       command=self._retry_failed, state="disabled")
        self.retry_button.pack(side="left")
        self._refresh_pending()

    def _refresh_pending(self, preferred_task_id=None):
        saved = [task for task in self.runtime.tasks.list()
                 if any(step.tool == "compose_send" for step in task.steps)
                 and task.status.value in {"waiting_approval", "completed", "failed"}]
        saved.sort(key=lambda task: task.updated_at, reverse=True)
        saved = saved[:25]
        values = [f"{task.id} | {task.status.value} | {task.prompt[:48]}" for task in saved]
        current_id = self.pending_task.get().split(" | ", 1)[0]
        self.pending_picker["values"] = values
        selected = next((value for value in values
                         if preferred_task_id and value.startswith(preferred_task_id + " | ")), "")
        if not selected:
            selected = next((value for value in values if value.startswith(current_id + " | ")), "")
        if not selected and getattr(self, "task_id", ""):
            selected = next((value for value in values
                             if value.startswith(self.task_id + " | ")), "")
        self.pending_task.set(selected or (values[0] if values else ""))
        if not values:
            self.pending_task.set("")
            self.preview.set("")
            self.retry_button.configure(state="disabled")
        else:
            self._show_selected_task()

    def _show_selected_task(self, _event=None):
        task_id = self.pending_task.get().split(" | ", 1)[0]
        task = self.runtime.tasks.get(task_id) if task_id else None
        if not task:
            self.preview.set("")
            return
        details = [f"Task {task.id} · {task.status.value}", task.prompt]
        step = next((item for item in reversed(task.steps) if item.tool == "compose_send"), None)
        if step:
            targets = step.args.get("targets", [])
            details.append("Destinations: " + ", ".join(
                f"{target.get('channel')} → {target.get('recipient')}" for target in targets
            ))
            details.append("Message to send: " + str(step.args.get("text", "")))
            voice_targets = [target for target in targets
                             if str(target.get("channel", "")).lower() == "voice"]
            if voice_targets:
                from .voice_webhook import is_public_https_url
                voice_url = os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "").strip()
                call_mode = ("conversational AI call" if is_public_https_url(voice_url)
                             else "one-way spoken call" if not voice_url
                             else "invalid voice callback configuration")
                details.append(f"VOICE: {call_mode}; caller identifies as an AI assistant. "
                               "Approval will place a real call to "
                               + ", ".join(str(target.get("recipient")) for target in voice_targets))
            payload = self._delivery_payload(task)
            if payload:
                details.append("Delivery outcomes: " + "; ".join(
                    f"{item.get('channel')}/{item.get('recipient')}: "
                    f"{'SENT' if item.get('ok') else 'FAILED'}"
                    + (f" — {item.get('detail')}" if item.get("detail") else "")
                    for item in payload.get("results", [])
                ))
        elif task.current_step < len(task.steps):
            step = task.steps[task.current_step]
            details.append(f"Next action: {step.tool} · {step.args}")
        if task.approval_reason:
            details.append("Approval requested: " + task.approval_reason)
        self.preview.set("\n".join(details))
        payload = self._delivery_payload(task)
        has_failures = bool(payload and any(not item.get("ok") for item in payload.get("results", [])))
        self.retry_button.configure(state="normal" if has_failures else "disabled")

    @staticmethod
    def _delivery_payload(task):
        for step in reversed(task.steps):
            if step.tool != "compose_send" or not step.result:
                continue
            try:
                payload = json.loads(step.result)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("kind") == "channel_delivery":
                return payload
        return None

    def _selected_task_id(self):
        selected = self.pending_task.get().strip()
        if selected:
            return selected.split(" | ", 1)[0]
        return getattr(self, "task_id", "")

    @staticmethod
    def validate_compose_text(text, targets):
        """Refuse text that the approval prompt would truncate."""
        limit = approval_text_limit("compose_send", {"targets": targets})
        if limit is None or len(text) <= limit:
            return
        if limit == 280:
            label = "X post"
        elif limit == 1200:
            label = "voice call opener"
        else:
            label = "message"
        raise ValueError(
            f"{label} cannot exceed {limit:,} characters because the approval "
            "prompt must show the complete text. Please shorten it and try again."
        )

    @staticmethod
    def validate_target(channel, recipient, supported_channels):
        channel = str(channel).strip().lower()
        recipient = str(recipient).strip()
        supported = {str(name).strip().lower() for name in supported_channels}
        if channel not in supported:
            raise ValueError(f"Unknown channel '{channel}'.")
        if not recipient:
            raise ValueError("Enter a recipient or destination for this channel.")
        return {"channel": channel, "recipient": recipient}

    def _add_destination(self):
        try:
            target = self.validate_target(
                self.destination_channel.get(), self.destination_recipient.get(),
                self.runtime.agent.hub.channels(),
            )
        except ValueError as exc:
            messagebox.showerror("Destination", str(exc))
            return
        if target in self.selected_targets:
            self.status.set("That destination is already in the list.")
            return
        self.selected_targets.append(target)
        self.destination_list.insert("", "end", values=(target["channel"], target["recipient"]))
        self.destination_recipient.set("")
        self.status.set(f"Added {target['channel']} destination.")

    def _remove_destination(self):
        selected = self.destination_list.selection()
        if not selected:
            return
        for item_id in selected:
            channel, recipient = self.destination_list.item(item_id, "values")
            target = {"channel": channel, "recipient": recipient}
            if target in self.selected_targets:
                self.selected_targets.remove(target)
            self.destination_list.delete(item_id)

    def _open_whatsapp(self):
        self.status.set("Opening Friday's persistent browser profile…")
        self.root.update_idletasks()
        try:
            from friday.mcp_server.browser import browser_open

            result = browser_open("https://web.whatsapp.com/")
            self.status.set(result)
        except Exception as exc:
            self.status.set("Could not open WhatsApp Web")
            messagebox.showerror("WhatsApp Web", str(exc))

    def _open_provider_settings(self):
        try:
            import subprocess

            subprocess.Popen(["notepad.exe", str(self.env_path)])
            self.status.set("Edit provider settings, save, then choose Refresh setup.")
        except Exception as exc:
            self.status.set("Could not open provider settings")
            messagebox.showerror("Provider settings", str(exc))

    def _refresh_setup(self):
        current_values = dotenv_values(self.env_path)
        current_keys = set(current_values)
        for key in self._dotenv_keys - current_keys:
            if key not in self._external_environment_keys:
                os.environ.pop(key, None)
        for key, value in current_values.items():
            if key in self._external_environment_keys:
                continue
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._dotenv_keys = current_keys
        self.readiness.set(
            f"Saved configuration (restart composer to apply): {self._readiness_summary()}"
        )
        self.status.set(
            "Configuration refreshed. Close and reopen the composer to rebuild channel adapters; "
            "restart the persistent agent too if webhook settings changed."
        )

    @staticmethod
    def _readiness_summary():
        import os
        from .voice_webhook import is_public_https_url

        required = {
            "telegram": ("TELEGRAM_BOT_TOKEN",),
            "slack": ("SLACK_BOT_TOKEN",),
            "discord": ("DISCORD_BOT_TOKEN",),
            "teams": ("TEAMS_WEBHOOK_URL",),
            "x": ("X_USER_ACCESS_TOKEN",),
            "x_public": ("X_USER_ACCESS_TOKEN",),
            "linkedin_public": ("LINKEDIN_ACCESS_TOKEN", "LINKEDIN_AUTHOR_URN"),
            "instagram": ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_ACCOUNT_ID"),
            "instagram_public": ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_ACCOUNT_ID"),
            "email": ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM"),
        }
        ready, missing = [], []
        if all(os.getenv(key) for key in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")):
            ready.append("whatsapp (Cloud API)")
        else:
            ready.append("whatsapp (Web; signed-in Chrome session required)")
        whatsapp_inbound_missing = [key for key in (
            "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_VERIFY_TOKEN", "META_APP_SECRET",
        ) if not os.getenv(key)]
        if not whatsapp_sender_allowlist_configured():
            whatsapp_inbound_missing.append("WHATSAPP_ALLOWED_SENDERS")
        if whatsapp_inbound_missing:
            lines = ["WhatsApp inbound: not configured; missing "
                     + ", ".join(whatsapp_inbound_missing)]
            if "WHATSAPP_ALLOWED_SENDERS" in whatsapp_inbound_missing:
                lines.append(
                    "Use Open provider settings to add trusted numbers as "
                    "+<countrycode><number>, comma-separated; then Refresh setup "
                    "and restart the persistent agent."
                )
        else:
            lines = ["WhatsApp inbound: credentials configured; public HTTPS webhook routing required"]
        for channel, keys in required.items():
            (ready if all(os.getenv(key) for key in keys) else missing).append(channel)
        if ready:
            lines.append("Available send paths: " + ", ".join(ready))
        if missing:
            lines.append("Needs provider credentials: " + ", ".join(missing))
        voice_missing = [key for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                                         "TWILIO_FROM_NUMBER") if not os.getenv(key)]
        voice_url = os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "").strip()
        if voice_missing:
            lines.append("voice: not configured; missing " + ", ".join(voice_missing))
        elif voice_url and is_public_https_url(voice_url):
            lines.append("voice: conversational mode configured; verify public webhook routing and LLM availability")
        elif voice_url:
            lines.append("voice: callback URL is invalid; use a public HTTPS URL")
        else:
            lines.append("voice: one-way spoken call; set PUBLIC_VOICE_WEBHOOK_URL for AI conversation")
        return "\n".join(lines)

    def _prepare(self):
        text = self.message.get("1.0", "end").strip()
        parsed = list(self.selected_targets)
        if not text or not parsed:
            messagebox.showerror("Missing information", "Enter a message and at least one destination.")
            return
        try:
            self.validate_compose_text(text, parsed)
        except ValueError as exc:
            messagebox.showerror("Message too long", str(exc))
            return
        self.send_button.configure(state="disabled")
        self.status.set("Preparing approval task…")
        self.root.update_idletasks()
        try:
            steps = [{"tool": "compose_send", "args": {"targets": parsed, "text": text},
                      "say": "Send composed message"}]
            task = self.runtime.tasks.create(
                text, steps, origin_channel="desktop", origin_sender_id="local-user",
                origin_conversation_id="compose-window",
            )
            self.task_id = task.id
            self.runtime.tasks.start(task.id)
            result = self.runtime.controller.advance(task.id)
            self.status.set(f"{result}  ·  selected: {len(parsed)}")
            self._refresh_pending(preferred_task_id=task.id)
        except Exception as exc:
            self.status.set("Could not prepare message")
            messagebox.showerror("Composer error", str(exc))
        finally:
            self.send_button.configure(state="normal")

    def _approve(self):
        task_id = self._selected_task_id()
        if not task_id:
            messagebox.showinfo("No pending task", "Prepare a message first.")
            return
        task = self.runtime.tasks.get(task_id)
        if not task or task.status.value != "waiting_approval":
            messagebox.showinfo("No approval pending", "The current task is not waiting for approval.")
            return
        voice_targets = []
        voice_text = ""
        for step in task.steps:
            if step.tool != "compose_send":
                continue
            step_voice_targets = [target for target in step.args.get("targets", [])
                                  if str(target.get("channel", "")).lower() == "voice"]
            if step_voice_targets:
                voice_targets.extend(step_voice_targets)
                voice_text = str(step.args.get("text", ""))
        if voice_targets:
            recipients = ", ".join(str(target.get("recipient")) for target in voice_targets)
            from .voice_webhook import is_public_https_url
            voice_url = os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "").strip()
            if is_public_https_url(voice_url):
                mode = "an AI assistant will converse about this purpose"
            elif voice_url:
                mode = "the configured callback is invalid, so the call will fail"
            else:
                mode = "the message will be spoken once"
            if not messagebox.askyesno(
                "Approve real phone call?",
                f"This will place a real {mode} to: {recipients}\n\n"
                "The call will identify the caller as an AI assistant. Approved opener/purpose:\n"
                f"{voice_text}\n\nPlace the call now?",
            ):
                return
        try:
            self.runtime.tasks.approve(task_id)
            self.status.set(self.runtime.controller.advance(task_id))
            self._refresh_pending()
        except Exception as exc:
            self.status.set("Approval action failed")
            messagebox.showerror("Send error", str(exc))

    def _cancel(self):
        task_id = self._selected_task_id()
        if not task_id:
            return
        task = self.runtime.tasks.get(task_id)
        if task and task.status.value == "waiting_approval":
            self.runtime.tasks.deny(task_id)
            self.status.set(f"Task {task_id} cancelled.")
            self._refresh_pending()

    def _retry_failed(self):
        task_id = self._selected_task_id()
        task = self.runtime.tasks.get(task_id) if task_id else None
        payload = self._delivery_payload(task) if task else None
        if not task or not payload:
            return
        failed = [item for item in payload.get("results", []) if not item.get("ok")]
        if not failed:
            return
        destinations = "\n".join(
            f"{item.get('channel')} → {item.get('recipient')}" for item in failed
        )
        approved = messagebox.askyesno(
            "Retry failed destinations?",
            "Only these destinations will be retried:\n\n" + destinations
            + "\n\nA provider may have accepted a message before returning an error; "
              "retrying could create duplicates. Continue?",
        )
        if not approved:
            return
        text = str(payload.get("text", ""))
        targets = [{"channel": item["channel"], "recipient": item["recipient"]}
                   for item in failed]
        try:
            self.validate_compose_text(text, targets)
        except ValueError as exc:
            messagebox.showerror("Message too long", str(exc))
            return
        steps = [{"tool": "compose_send", "args": {"targets": targets, "text": text},
                  "say": "Retry failed composed destinations"}]
        retry_task = self.runtime.tasks.create(
            f"Retry failed destinations from task {task.id}", steps,
            origin_channel="desktop", origin_sender_id="local-user",
            origin_conversation_id="compose-window",
        )
        self.task_id = retry_task.id
        self.runtime.tasks.start(retry_task.id)
        self.status.set(self.runtime.controller.advance(retry_task.id))
        self._refresh_pending(preferred_task_id=retry_task.id)

    def run(self):
        self.root.mainloop()


def main():
    ComposeWindow().run()


if __name__ == "__main__":
    main()
