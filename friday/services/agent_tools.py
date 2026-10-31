"""Register existing Windows capabilities as policy-aware agent tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .communication_hub import CommunicationHub
from .memory_store import MemoryStore
from .orchestrator import StepPolicy, TaskOrchestrator
from .workers import WorkerRegistry


def _parse_bool(value: Any) -> bool:
    """Parse explicit boolean values; unknown strings never trigger actions."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return False


def _agent_file_path(raw: str | None) -> Path:
    configured_roots = [item.strip() for item in
                        os.getenv("AGENT_FILE_ROOTS", "").split(os.pathsep)
                        if item.strip()]
    roots = ([Path(item).expanduser().resolve() for item in configured_roots]
             if configured_roots else [Path.home().resolve()])
    # With an explicit allowlist, default searches should start inside it
    # rather than defaulting to a home directory that may be outside the roots.
    target = Path(raw).expanduser() if raw else roots[0]
    target = target.resolve()
    if not any(target == root or root in target.parents for root in roots):
        raise ValueError("path is outside the configured agent file roots")
    return target


def register_agent_tools(orchestrator: TaskOrchestrator, hub: CommunicationHub,
                         workers: WorkerRegistry, memory: MemoryStore) -> None:
    capabilities = {
        "communication": {"communication"},
        "browser": {"browser", "research"},
        "desktop": {"desktop"},
        "files": {"files"},
        "memory": {"memory"},
        "calendar": {"calendar"},
    }
    for name, items in capabilities.items():
        if name not in workers._items:
            workers.register(name, items)
        else:
            workers._items[name].capabilities.update(items)
        workers.set_online(name)

    def send(args):
        channel = str(args["channel"])
        recipient = str(args["recipient"])
        result = hub.send(channel, recipient, str(args["text"]))
        if not result.ok:
            raise RuntimeError(result.detail or f"delivery failed on {channel}")
        return result.detail or "sent"

    def compose_send(args):
        targets = args.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ValueError("targets must be a non-empty list of {channel, recipient} objects")
        normalized = []
        for target in targets:
            if not isinstance(target, dict) or not target.get("channel") or not target.get("recipient"):
                raise ValueError("each target needs a channel and recipient")
            normalized.append((target["channel"], target["recipient"]))
        results = hub.send_many(normalized, str(args["text"]))
        if not results:
            raise ValueError("no valid channel recipients were provided")
        # A fan-out is not atomic: some providers may accept a message before
        # another rejects it. Persist structured per-destination outcomes so
        # the composer can offer a retry of failures only.
        return json.dumps({
            "kind": "channel_delivery",
            "text": str(args["text"]),
            "results": [{"channel": item.channel, "recipient": item.conversation_id,
                         "ok": item.ok, "detail": item.detail} for item in results],
        }, ensure_ascii=False)

    def respond(args):
        """Return text only to the conversation that invoked this task."""
        if "memory_query" in args:
            items = memory.search(str(args["memory_query"]),
                                  scope=str(args.get("_memory_scope", "owner")))
            if not items:
                return "I don't have any saved memory about that."
            facts = "; ".join(item["text"] for item in items)
            return ("Here's what I remember: " + facts)[:4000]
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValueError("respond requires non-empty text")
        return text[:4000]

    def remember(args):
        item = memory.remember(str(args["text"]), kind=str(args.get("kind", "fact")),
                               scope=str(args.get("_memory_scope", "owner")))
        return item["text"]

    def recall(args):
        return "\n".join(item["text"] for item in memory.search(
            str(args["query"]), scope=str(args.get("_memory_scope", "owner")))) \
            or "No saved memory found."

    def search_agent_files(args):
        directory = _agent_file_path(args.get("directory"))
        return files.search_files(str(args["query"]), str(directory))

    def open_agent_path(args):
        path = _agent_file_path(str(args["path"]))
        return files.open_path(str(path))

    orchestrator.register_tool("send", StepPolicy("communication", requires_approval=True), send)
    orchestrator.register_tool("compose_send", StepPolicy("communication", requires_approval=True),
                               compose_send)
    orchestrator.register_tool("respond", StepPolicy("communication"), respond)

    from .channel_adapters import XAdapter
    x_adapter = XAdapter()
    def x_post(args):
        result = x_adapter.create_post(str(args["text"]))
        if "not configured" in result.lower():
            raise RuntimeError(result)
        return result
    orchestrator.register_tool("x_post", StepPolicy("communication", requires_approval=True),
        x_post)
    orchestrator.register_tool("remember", StepPolicy("memory"), remember)
    orchestrator.register_tool("recall", StepPolicy("memory"), recall)

    from friday.mcp_server import apps, browser, calendar, files
    orchestrator.register_tool("web_search", StepPolicy("research"),
        lambda args: apps.web_search(str(args["query"])))
    orchestrator.register_tool("browser_open", StepPolicy("browser"),
        lambda args: browser.browser_open(str(args["url"])))
    orchestrator.register_tool("browser_read", StepPolicy("browser"),
        lambda _args: browser.browser_read())
    orchestrator.register_tool("browser_inspect", StepPolicy("browser"),
        lambda _args: getattr(browser, "browser_inspect_a11y", browser.browser_read)())
    orchestrator.register_tool("browser_click", StepPolicy("browser", requires_approval=True),
        lambda args: browser.browser_click(str(args["text"])))
    orchestrator.register_tool("browser_fill", StepPolicy("browser", requires_approval=True),
        lambda args: browser.browser_fill(str(args["label"]), str(args["value"]),
                                          _parse_bool(args.get("submit", False))))
    orchestrator.register_tool("search_files", StepPolicy("files"),
        search_agent_files)
    orchestrator.register_tool("open_path", StepPolicy("files", requires_approval=True),
        open_agent_path)
    orchestrator.register_tool("calendar_add", StepPolicy("calendar", requires_approval=True),
        lambda args: calendar.calendar_add(str(args["title"]), str(args["when"]),
                                           int(args.get("duration_minutes", 60))))
