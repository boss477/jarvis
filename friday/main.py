"""Friday v1 — local push-to-talk voice assistant.

Pipeline: SPACE (PTT) -> Whisper STT -> OpenRouter LLM -> Supertonic TTS -> speakers.
No computer-control tools yet (added later via MCP).
"""

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv
from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.openrouter.llm import OpenRouterLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.mcp_service import MCPClient
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.services.whisper.stt import WhisperSTTService
from mcp.client.session_group import StreamableHttpParameters
from pipecat.transcriptions.language import Language
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from services.ptt import PushToTalkProcessor
from services.kokoro_tts import KokoroTTSService

load_dotenv(HERE / ".env")

SYSTEM_PROMPT = (
    "You are Friday, a local voice assistant. Reply in one short sentence only — "
    "never more. Be direct. You are spoken aloud, so never use markdown, bullet "
    "points, code blocks, or emoji. You can control this computer with tools. If a "
    "tool reply starts with CONFIRM_REQUIRED, read the action aloud and ask the user "
    "to say confirm or cancel; when they confirm, call confirm_action with the id "
    "shown in brackets; if they decline, call cancel_action."
)


def _trunc(result):
    """Cap noisy tool output before it reaches the LLM context."""
    s = result if isinstance(result, str) else str(result)
    return s if len(s) <= 2000 else s[:2000] + " …(truncated)"


async def main():
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        )
    )

    stt = WhisperSTTService(
        model=os.getenv("WHISPER_MODEL", "large-v3-turbo"),
        device=os.getenv("WHISPER_DEVICE", "cpu"),
        compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        language=Language.EN,
    )

    # LLM provider is chosen by LLM_BASE_URL: OpenRouter (cloud) vs any local
    # OpenAI-compatible server (LM Studio :1234, Ollama :11434). Both speak the
    # OpenAI API, so tools/function-calling work the same way.
    llm_base_url = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    if "openrouter" in llm_base_url:
        llm = OpenRouterLLMService(
            api_key=os.environ["OPENROUTER_API_KEY"],
            model=os.getenv("LLM_MODEL", "nex-agi/nex-n2-pro:free"),
        )
    else:
        llm = OpenAILLMService(
            api_key=os.getenv("LLM_API_KEY", "local"),
            base_url=llm_base_url,
            model=os.getenv("LLM_MODEL", "local-model"),
        )

    tts = KokoroTTSService(voice="am_michael")

    # Connect to the standalone tool server and register its tools on the LLM.
    # run_friday.ps1 starts the server first; if it isn't reachable, degrade
    # gracefully to a plain voice assistant rather than crashing. (CancelledError
    # is caught too: pipecat's connect surfaces one on a cold port.)
    mcp = MCPClient(
        StreamableHttpParameters(url=os.environ["MCP_URL"]),
        tools_output_filters={"run_powershell": _trunc, "search_files": _trunc},
    )
    tools = None
    try:
        await mcp.start()
        tools = await mcp.register_tools(llm)
        logger.info("Tool server connected — computer-control tools enabled.")
    except (Exception, asyncio.CancelledError) as e:
        logger.warning(
            f"Tool server unreachable at {os.environ['MCP_URL']} ({type(e).__name__}); "
            "running WITHOUT computer-control tools. Use run_friday.ps1 to enable them."
        )
        mcp = None

    if tools is not None:
        context = LLMContext(
            messages=[{"role": "system", "content": SYSTEM_PROMPT}],
            tools=tools,
        )
    else:
        context = LLMContext(messages=[{"role": "system", "content": SYSTEM_PROMPT}])
    # PTT is the sole turn authority: turns start/stop on the frames our
    # PushToTalkProcessor emits — no VAD, no smart-turn analyzer, no silence timer.
    agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=ExternalUserTurnStrategies()
        ),
    )
    ptt = PushToTalkProcessor()

    pipeline = Pipeline(
        [
            transport.input(),
            ptt,
            stt,
            agg.user(),
            llm,
            tts,
            transport.output(),
            agg.assistant(),
        ]
    )

    task = PipelineTask(pipeline, params=PipelineParams())
    runner = PipelineRunner(handle_sigint=True)
    logger.info("Friday online. Hold SPACE, speak, release. Ctrl+C to quit.")
    try:
        await runner.run(task)
    finally:
        if mcp is not None:
            await mcp.close()  # session must live the whole app; close only on shutdown


if __name__ == "__main__":
    asyncio.run(main())
