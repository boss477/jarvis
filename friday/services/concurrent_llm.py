"""Concurrent tool scheduler for Jarvis.

Implements the Claude Code batch-scheduling algorithm:
  - Consecutive concurrency-safe tool calls → single asyncio.gather() batch
  - Any unsafe tool → forces serial execution (awaited alone)

This means "list_windows + system_info + browser_read" runs in parallel,
while "system_info + delete_path" runs system_info first, then delete_path.

Wire-up: use ConcurrentOpenAILLMService / ConcurrentOpenRouterLLMService
instead of their plain counterparts in main.py.
"""

import asyncio
import uuid
from typing import Sequence

from loguru import logger
from pipecat.frames.frames import FunctionCallsStartedFrame
from pipecat.services.llm_service import FunctionCallFromLLM, FunctionCallRunnerItem
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openrouter.llm import OpenRouterLLMService
from pipecat.utils.async_tool_cancellation import CANCEL_ASYNC_TOOL_NAME

from mcp_server.concurrency import CONCURRENCY_SAFE_TOOLS


class ConcurrentToolMixin:
    """Override run_function_calls with a batch-aware parallel scheduler."""

    async def run_function_calls(self, function_calls: Sequence[FunctionCallFromLLM]):
        if not function_calls:
            return

        user_visible = [fc for fc in function_calls if fc.function_name != CANCEL_ASYNC_TOOL_NAME]
        if user_visible:
            await self._call_event_handler("on_function_calls_started", user_visible)
            await self.broadcast_frame(FunctionCallsStartedFrame, function_calls=user_visible)

        # All calls in one LLM turn share a group_id so the aggregator triggers
        # the LLM exactly once after the last result arrives.
        group_id = str(uuid.uuid4()) if self._group_parallel_tools else None

        # Build runner items (same logic as LLMService.run_function_calls).
        runner_items: list[FunctionCallRunnerItem] = []
        for fc in function_calls:
            if fc.function_name in self._functions:
                item = self._functions[fc.function_name]
            elif None in self._functions:
                item = self._functions[None]
            else:
                self._log_missing_function_call(fc.function_name, fc.context)
                item = self._build_missing_function_call_registry_item(fc.function_name)
            runner_items.append(FunctionCallRunnerItem(
                registry_item=item,
                function_name=fc.function_name,
                tool_call_id=fc.tool_call_id,
                arguments=fc.arguments,
                context=fc.context,
                group_id=group_id,
            ))

        # Partition: consecutive safe tools merge into one parallel batch.
        batches: list[dict] = []
        for ri in runner_items:
            is_safe = ri.function_name in CONCURRENCY_SAFE_TOOLS
            if batches and batches[-1]["safe"] and is_safe:
                batches[-1]["items"].append(ri)
            else:
                batches.append({"safe": is_safe, "items": [ri]})

        # Execute batches in order. Safe multi-tool batches run concurrently.
        for batch in batches:
            items = batch["items"]
            if batch["safe"] and len(items) > 1:
                names = [ri.function_name for ri in items]
                logger.info(f"[tool-scheduler] parallel batch: {names}")
                await asyncio.gather(*[self._run_function_call(ri) for ri in items])
            else:
                for ri in items:
                    logger.debug(f"[tool-scheduler] serial: {ri.function_name}")
                    await self._run_function_call(ri)


class ConcurrentOpenAILLMService(ConcurrentToolMixin, OpenAILLMService):
    pass


class ConcurrentOpenRouterLLMService(ConcurrentToolMixin, OpenRouterLLMService):
    pass
