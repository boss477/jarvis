"""Friday v1 — local push-to-talk voice assistant.

Pipeline: SPACE (PTT) -> Whisper STT -> OpenRouter LLM -> Kokoro TTS -> speakers.
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
from services.concurrent_llm import ConcurrentOpenAILLMService, ConcurrentOpenRouterLLMService
from services.mcp_client import JarvisMCPClient as MCPClient
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.services.whisper.stt import WhisperSTTService
from mcp.client.session_group import StreamableHttpParameters
from pipecat.transcriptions.language import Language
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from pipecat.services.openai.llm import OpenAILLMService

from services.alias_processor import AliasProcessor
from services.barge_in import BargeInProcessor
from services.kokoro_tts import KokoroTTSService
from services.overlay import JarvisOverlay, OverlayStateReporter
from services.plan_processor import PlanProcessor
from services.ptt import PushToTalkProcessor
from services.transcript_filter import TranscriptFilter
from services.vision import VisionContextInjector, VisionPTTProcessor, VisionState
from services.wakeword import WakeWordProcessor

load_dotenv(HERE / ".env")

SYSTEM_PROMPT = (
    # Running Gemma (google/gemma-4-12b-qat), which has no reasoning/thinking mode,
    # so no soft switch is needed. (Qwen3 used a "/no_think" prefix here to disable
    # its reasoning pass; Gemma would read that token as literal text, so it's gone.)
    "You are Jarvis, a local voice assistant with the calm, composed wit of Tony "
    "Stark's AI. Reply in one short sentence only — never more. Be direct, warm, and "
    "use natural contractions; the user is 'boss'. Occasional 'on it' or 'affirmative' "
    "is fine — never robotic. When the user greets you, greet back to match the time "
    "of day given below. You are spoken aloud, so never use markdown, bullet "
    "points, code blocks, or emoji. Tools run silently: NEVER speak tool names, "
    "function calls, JSON, arguments, or raw output aloud — call the tool, then narrate "
    "the result in plain conversational speech. You can control this computer with tools. If a "
    "tool reply starts with CONFIRM_REQUIRED, read the action aloud and ask the user "
    "to say confirm or cancel; when they confirm, call confirm_action with the id "
    "shown in brackets; if they decline, call cancel_action. To play a specific song "
    "or artist, ALWAYS call spotify_play with the name — never media_control, which "
    "only pauses or resumes whatever is already playing. Never claim you did something "
    "unless a tool actually returned success; if a tool reports an error, say what it "
    "said. For any task that has no "
    "dedicated tool, accomplish it by writing one PowerShell command and calling "
    "run_powershell. "
    "When an image is attached it is a screenshot of the user's screen — describe what "
    "you actually see in it; never say you cannot see the screen."
)


def _system_prompt() -> str:
    """SYSTEM_PROMPT plus the current local time, so greetings adapt to the
    time of day (morning/afternoon/evening). Built per run, not per turn."""
    from datetime import datetime

    h = datetime.now().hour
    part = "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    return f"{SYSTEM_PROMPT} It is currently {part} (local time {datetime.now():%H:%M})."


def _trunc(result):
    """Cap noisy tool output before it reaches the LLM context."""
    s = result if isinstance(result, str) else str(result)
    return s if len(s) <= 2000 else s[:2000] + " …(truncated)"


def _server_reachable(url: str) -> bool:
    """Quick TCP check so we never call MCPClient.start() against a cold port —
    a failed start() leaves an orphaned anyio cancel scope that later crashes
    the event loop."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=0.5):
            return True
    except OSError:
        return False


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
        llm = ConcurrentOpenRouterLLMService(
            api_key=os.environ["OPENROUTER_API_KEY"],
            model=os.getenv("LLM_MODEL", "nex-agi/nex-n2-pro:free"),
        )
    else:
        llm = ConcurrentOpenAILLMService(
            api_key=os.getenv("LLM_API_KEY", "local"),
            base_url=llm_base_url,
            model=os.getenv("LLM_MODEL", "local-model"),
            # Gemma-4-qat reasons by default, burning the (tiny) token budget on
            # thinking and returning empty content. reasoning_effort="none" is the
            # only switch that disables it for this LM Studio build (verified).
            params=OpenAILLMService.InputParams(extra={"reasoning_effort": "none"}),  # disable Gemma reasoning
        )

    tts = KokoroTTSService(voice="am_michael")

    # Connect to the standalone tool server and register its tools on the LLM.
    # We only call start() when the port is actually open: a failed start()
    # against a cold server poisons the event loop (orphaned anyio cancel scope
    # → later crash). If the server is down, run as a plain voice assistant.
    mcp_url = os.environ["MCP_URL"]
    mcp = None
    tools = None
    if _server_reachable(mcp_url):
        try:
            mcp = MCPClient(
                StreamableHttpParameters(url=mcp_url),
                tools_output_filters={
            "run_powershell": _trunc,
            "search_files": _trunc,
            "list_windows": _trunc,
            "list_elements": _trunc,
            "browser_read": _trunc,
        },
            )
            await mcp.start()
            tools = await mcp.register_tools(llm)
            logger.info("Tool server connected — computer-control tools enabled.")
        except Exception as e:
            logger.warning(f"Tool server handshake failed ({type(e).__name__}); running without tools.")
            mcp = None
            tools = None
    else:
        logger.warning(
            f"Tool server not reachable at {mcp_url}; running WITHOUT computer-control "
            "tools. Use run_friday.ps1 to start the server first."
        )

    sys_prompt = _system_prompt()
    if tools is not None:
        context = LLMContext(
            messages=[{"role": "system", "content": sys_prompt}],
            tools=tools,
        )
    else:
        context = LLMContext(messages=[{"role": "system", "content": sys_prompt}])
    # PTT is the sole turn authority: turns start/stop on the frames our
    # PushToTalkProcessor emits — no VAD, no smart-turn analyzer, no silence timer.
    agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=ExternalUserTurnStrategies()
        ),
    )
    vision_state = VisionState()
    ptt = PushToTalkProcessor(vision_state=vision_state)
    vision_ptt = VisionPTTProcessor(state=vision_state, key_name=os.getenv("VISION_KEY", "f9"))
    vision_injector = VisionContextInjector(state=vision_state)

    # Fast-path processors (only active when MCP server is up)
    plan_proc = None
    alias_proc = None
    if mcp is not None:
        plan_proc = PlanProcessor(
            mcp_url=mcp_url,
            llm_base_url=llm_base_url,
            llm_model=os.getenv("LLM_MODEL", "local-model"),
            llm_api_key=os.getenv("LLM_API_KEY", "local"),
        )
        alias_proc = AliasProcessor(mcp_url=mcp_url)

    # Floating status circle (idle / listening / thinking / speaking). Disable with OVERLAY=0.
    overlay_reporter = None
    if os.getenv("OVERLAY", "1") != "0":
        overlay = JarvisOverlay()
        overlay.start()
        overlay_reporter = OverlayStateReporter(overlay)

    # Barge-in: interrupt an in-progress reply when a new turn starts (SPACE / wake /
    # vision). No-op unless the bot is actually speaking. Disable with BARGE_IN=0.
    barge_in = BargeInProcessor() if os.getenv("BARGE_IN", "1") != "0" else None

    # Hands-free wake word ("hey jarvis") alongside SPACE. Sits after PTT so it
    # observes PTT turns and stays dormant during them; Silero VAD ends the
    # wake-word turn on silence. Disable with WAKE_WORD=0.
    wake = None
    if os.getenv("WAKE_WORD", "1") != "0":
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams

        # Endpointing delay: silence (secs) before the wake-word turn is considered
        # over. Lower = snappier hands-free replies but more risk of cutting off a
        # mid-sentence pause. Only affects the "hey jarvis" path (PTT has no VAD).
        wake = WakeWordProcessor(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=VADParams(stop_secs=float(os.getenv("ENDPOINT_DELAY", "0.2"))),
            ),
            threshold=float(os.getenv("WAKE_THRESHOLD", "0.3")),
            model_name=os.getenv("WAKE_MODEL", "hey_jarvis"),
        )

    pipeline = Pipeline(
        [
            p for p in [
                transport.input(),
                overlay_reporter,  # status circle: listening/thinking/speaking
                ptt,
                wake,
                vision_ptt,
                barge_in,
                stt,
                TranscriptFilter(),
                agg.user(),
                vision_injector,
                plan_proc,    # multi-step: plan → execute → summarize (bypasses LLM)
                alias_proc,   # single-action: pattern match → execute → reply (bypasses LLM)
                llm,          # everything else
                tts,
                transport.output(),
                agg.assistant(),
            ] if p is not None
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
