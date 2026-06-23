"""
Alias processor: fast-path common single-action voice commands, bypassing the LLM.

Saves 2-3 s per simple command by skipping LLM inference entirely.
Sits between vision_injector and plan_processor in the pipeline.

Only fires on clearly-single-action commands (no "and then" / "after that" conjunctions).
On any match failure or MCP error it falls through to the LLM unchanged.
"""

import re
from datetime import datetime
from loguru import logger

from fastmcp import Client as MCPClient
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Skip alias matching when the command looks multi-step
_MULTI_STEP = re.compile(r"\band\s+then\b|\bafter\s+that\b|\band\s+also\b|\bthen\s+also\b", re.I)


def _user_text(context) -> str:
    """Extract the last user message text from the LLM context."""
    for msg in reversed(context.messages):
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"
                )
    return ""


# Each entry: (compiled_pattern, tool_name_or_None, arg_fn, reply_fn)
# tool_name=None means no MCP call needed (pure Python response).
_ALIASES: list[tuple] = [
    # Time query — no tool needed
    (
        re.compile(r"\bwhat(?:'s|\s+is)\s+the\s+time\b", re.I),
        None,
        lambda m: {},
        lambda _, m: datetime.now().strftime("It's %I:%M %p").lstrip("0"),
    ),
    # Volume
    (
        re.compile(r"\bset\s+(?:the\s+)?volume\s+to\s+(\d+)", re.I),
        "set_volume",
        lambda m: {"level": int(m.group(1))},
        lambda _, m: f"Volume set to {m.group(1)}",
    ),
    # Mute (strict: whole command is just "mute [the sound/audio]")
    (
        re.compile(r"^(?:please\s+)?mute(?:\s+(?:the\s+)?(?:sound|audio))?\s*$", re.I),
        "toggle_mute",
        lambda m: {},
        lambda _, m: "Muted",
    ),
    # Unmute
    (
        re.compile(r"^(?:please\s+)?unmute(?:\s+(?:the\s+)?(?:sound|audio))?\s*$", re.I),
        "toggle_mute",
        lambda m: {},
        lambda _, m: "Unmuted",
    ),
    # Pause playback
    (
        re.compile(r"^(?:please\s+)?pause(?:\s+(?:the\s+)?(?:music|playback|audio))?\s*$", re.I),
        "media_control",
        lambda m: {"action": "pause"},
        lambda _, m: "Paused",
    ),
    # Resume / unpause
    (
        re.compile(
            r"^(?:please\s+)?(?:resume|unpause)(?:\s+(?:the\s+)?(?:music|playback|audio))?\s*$",
            re.I,
        ),
        "media_control",
        lambda m: {"action": "resume"},
        lambda _, m: "Resumed",
    ),
    # Next track
    (
        re.compile(r"\bnext\s+(?:track|song)\b", re.I),
        "media_control",
        lambda m: {"action": "next"},
        lambda _, m: "Next track",
    ),
    # Previous track
    (
        re.compile(r"\bprevious\s+(?:track|song)\b", re.I),
        "media_control",
        lambda m: {"action": "previous"},
        lambda _, m: "Previous track",
    ),
    # Lock screen
    (
        re.compile(r"\block\s+(?:the\s+)?(?:screen|computer|pc)\b", re.I),
        "lock_screen",
        lambda m: {},
        lambda _, m: "Locking the screen",
    ),
    # Brightness
    (
        re.compile(r"\bset\s+(?:the\s+)?brightness\s+to\s+(\d+)", re.I),
        "set_brightness",
        lambda m: {"level": int(m.group(1))},
        lambda _, m: f"Brightness set to {m.group(1)}",
    ),
]


class AliasProcessor(FrameProcessor):
    """Intercept simple single-action commands and execute them without the LLM."""

    def __init__(self, mcp_url: str, **kwargs):
        super().__init__(**kwargs)
        self._mcp_url = mcp_url

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        text = _user_text(frame.context).strip()

        # Let multi-step commands pass through to the plan processor / LLM
        if _MULTI_STEP.search(text):
            await self.push_frame(frame, direction)
            return

        for pattern, tool, arg_fn, reply_fn in _ALIASES:
            m = pattern.search(text)
            if not m:
                continue

            tool_result = None
            if tool:
                try:
                    async with MCPClient(self._mcp_url) as client:
                        tool_result = str(await client.call_tool(tool, arg_fn(m)))
                except Exception as exc:
                    logger.warning(f"[alias] tool '{tool}' failed ({exc}); falling through to LLM")
                    await self.push_frame(frame, direction)
                    return

            reply = reply_fn(tool_result, m)
            logger.info(f"[alias] '{text[:50]}' → {tool} → '{reply}'")

            # Emit the response: TTS speaks it, agg.assistant() records it in context
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(TextFrame(text=reply))
            await self.push_frame(LLMFullResponseEndFrame())
            return  # LLMContextFrame is NOT forwarded — LLM is skipped

        # No alias matched — let the LLM handle it
        await self.push_frame(frame, direction)
