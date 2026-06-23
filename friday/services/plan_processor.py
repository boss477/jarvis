"""
Plan processor: handle multi-step voice commands with a Plan → Execute → Summarize loop.

For commands like "open Chrome and then play lo-fi music" it:
  1. Makes a small, fast LLM call to produce a JSON plan (steps with tool + args)
  2. Executes each step in sequence via MCP
  3. Speaks a one-sentence spoken summary of what happened

This is faster than the default path for multi-step tasks because:
  - The planning prompt is tiny → fast LLM response (~0.5–1 s)
  - No second LLM call to summarize (we build the summary from step results)
  - Steps run directly via MCP without the LLM overhead per step

Sits between vision_injector and alias_processor in the pipeline.
Falls through to the LLM if plan generation fails, returns bad JSON, or is not multi-step.
"""

import json
import re

import httpx
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

_MULTI_STEP = re.compile(
    r"\band\s+then\b|\bafter\s+that\b|\band\s+also\b|\bthen\s+also\b|\bfirst\b.{1,40}\bthen\b",
    re.I | re.DOTALL,
)

_TOOL_LIST = (
    "set_volume(level:int 0-100), toggle_mute(), open_app(name:str), open_url(url:str), "
    "media_control(action:pause|resume|next|previous), lock_screen(), "
    "set_brightness(level:int 0-100), spotify_play(query:str), web_search(query:str), "
    "run_powershell(command:str), schedule_task(name:str,command:str,schedule:str)"
)

_PLAN_SYSTEM = (
    "You are a planning engine. Given a voice command, output ONLY a JSON array of steps. "
    "No explanation, no markdown fences, just the raw JSON array. "
    f"Available tools: {_TOOL_LIST}"
)

_PLAN_USER = (
    'Command: "{text}"\n\n'
    'Output format: [{{"tool":"name","args":{{}},"say":"short spoken phrase"}}]'
)


def _extract_json(text: str) -> list:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Model may have wrapped the array in backticks or added commentary
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"No JSON array in LLM output: {text[:200]!r}")


def _user_text(context) -> str:
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


class PlanProcessor(FrameProcessor):
    """Decompose multi-step commands into a JSON plan and execute them directly."""

    def __init__(
        self,
        mcp_url: str,
        llm_base_url: str,
        llm_model: str,
        llm_api_key: str,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._mcp_url = mcp_url
        self._llm_url = llm_base_url.rstrip("/")
        self._llm_model = llm_model
        self._llm_key = llm_api_key

    async def _make_plan(self, user_text: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=12.0) as http:
            resp = await http.post(
                f"{self._llm_url}/chat/completions",
                json={
                    "model": self._llm_model,
                    "messages": [
                        {"role": "system", "content": _PLAN_SYSTEM},
                        {"role": "user", "content": _PLAN_USER.format(text=user_text)},
                    ],
                    "temperature": 0,
                    "max_tokens": 400,
                },
                headers={"Authorization": f"Bearer {self._llm_key}"},
            )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _extract_json(content)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        text = _user_text(frame.context).strip()

        if not _MULTI_STEP.search(text):
            await self.push_frame(frame, direction)
            return

        # Try planning
        try:
            steps = await self._make_plan(text)
            if not steps or not isinstance(steps, list):
                raise ValueError("Plan is empty or not a list")
        except Exception as exc:
            logger.warning(f"[plan] Plan generation failed ({exc}); falling through to LLM")
            await self.push_frame(frame, direction)
            return

        logger.info(f"[plan] {len(steps)}-step plan for: '{text[:60]}'")

        successes: list[str] = []
        failures: list[str] = []

        async with MCPClient(self._mcp_url) as client:
            for step in steps:
                tool = step.get("tool", "")
                args = step.get("args", {})
                say = step.get("say", tool)
                if not tool:
                    continue
                try:
                    await client.call_tool(tool, args)
                    successes.append(say)
                    logger.info(f"[plan] ✓ {tool}({args})")
                except Exception as exc:
                    failures.append(say)
                    logger.warning(f"[plan] ✗ {tool}({args}): {exc}")

        # Build a spoken summary
        if not failures:
            summary = "All done."
        elif not successes:
            summary = "Sorry, none of the steps worked."
        else:
            summary = f"Done, but I couldn't {' or '.join(failures)}."

        logger.info(f"[plan] summary: {summary!r}")
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(TextFrame(text=summary))
        await self.push_frame(LLMFullResponseEndFrame())
        # LLMContextFrame is intentionally NOT forwarded — LLM is skipped
