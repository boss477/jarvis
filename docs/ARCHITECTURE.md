# Jarvis — Local Voice Assistant: Architecture (0 → 1)

A from-scratch guide to what this project is, the tech stack it uses, and how every
piece fits together. Written so someone who has never seen the repo can understand
the whole thing top to bottom.

> The project folder is `jarrrrr/`, the Python package is `friday/`, and the
> assistant's spoken name is **Jarvis** (it was renamed from "Friday"; you'll see all
> three names around the codebase — they're the same thing).

---

## 1. What is this?

Jarvis is a **fully local, voice-driven assistant that can control your Windows PC**.
You talk to it; it talks back; and it can actually *do things* — open apps, change the
volume, search files, run PowerShell, drive a browser, play Spotify, look at your
screen, and more.

Two things make it interesting:

1. **Everything that can be local, is local.** Speech-to-text, text-to-speech, and
   (optionally) the language model all run on your own machine and GPU. No cloud round
   trip on the hot path. The single design goal that overrides everything else is
   **low latency** — it should feel like talking to a person, not filing a request.

2. **It controls the real computer through a tool server**, not screen-scraping or
   blind clicking. Each capability is a real API/library call exposed as a "tool" the
   model can invoke.

### The core loop, in one sentence

> You **hold SPACE** (or **say "hey jarvis"**) → your speech is transcribed → a language
> model decides what to say and which tools to call → the tools run on your PC → the
> reply is spoken back through your speakers.

```
 ┌─────────┐   audio   ┌──────────┐  text   ┌─────────┐  tool calls  ┌──────────────┐
 │   Mic   │ ───────▶ │ Whisper  │ ──────▶ │   LLM   │ ───────────▶ │  Tool server │
 │ (SPACE/ │           │  (STT)   │         │ (brain) │ ◀─────────── │  (your PC)   │
 │  wake)  │           └──────────┘         └────┬────┘   results    └──────────────┘
 └─────────┘                                     │ reply text
                                                 ▼
                                           ┌──────────┐  audio   ┌──────────┐
                                           │  Kokoro  │ ───────▶ │ Speakers │
                                           │  (TTS)   │          └──────────┘
                                           └──────────┘
```

---

## 2. Tech stack at a glance

| Layer | Technology | Why it's here |
|---|---|---|
| **Orchestrator** | [Pipecat](https://github.com/pipecat-ai/pipecat) (`pipecat-ai`) | Streams audio/text through a pipeline of "frame processors"; wires mic → STT → LLM → TTS → speakers. |
| **Speech-to-text (STT)** | `faster-whisper` (`large-v3-turbo`) on **GPU** | Fast, accurate local transcription. |
| **Language model (LLM)** | **Local via LM Studio** (OpenAI-compatible) — `google/gemma-4-12b-qat`; can also use **OpenRouter** (cloud) | The "brain": decides replies and tool calls. Switchable via one env var. |
| **Text-to-speech (TTS)** | [Kokoro](https://github.com/thewh1teagle/kokoro-onnx) ONNX on **GPU** (voice `am_michael`) | ~0.2–0.3 s synthesis, natively 24 kHz (no resampling). |
| **Wake word** | [openWakeWord](https://github.com/dscripka/openWakeWord) (`hey_jarvis` ONNX) + Silero VAD | Hands-free "hey jarvis" trigger; tiny CPU model. |
| **Tool server** | [FastMCP](https://github.com/jlowin/fastmcp) over HTTP (MCP protocol) | Hosts all 39 computer-control tools as one process. |
| **Computer control** | `pywinauto` (UI Automation), `psutil`, `pycaw`, ctypes/user32, `pynput`, `subprocess`/`schtasks`, [Playwright](https://playwright.dev), [spotipy](https://github.com/spotipy-dev/spotipy) | Each is a real API the model calls — never blind GUI clicking. |
| **Screen vision** | Pillow `ImageGrab` → JPEG data URL → multimodal LLM message | Hold a key, ask about what's on screen. |
| **GPU plumbing** | PyTorch `cu121` wheels supply CUDA 12 + cuDNN 9 DLLs for `onnxruntime-gpu` | Runs GPU inference **without installing the CUDA Toolkit**. |
| **Runtime / packaging** | Python 3.11, [`uv`](https://github.com/astral-sh/uv) | Fast, reproducible env from `pyproject.toml` + `uv.lock`. |
| **Config** | `python-dotenv` (`friday/.env`) | All secrets + tunables live in one gitignored file. |
| **Logging** | `loguru` | Readable structured logs. |

**Machine reality this was built on:** Windows 11, RTX 4060 (8 GB VRAM) + 24 GB RAM,
*no* CUDA Toolkit and *no* C++ compiler installed. That constraint shaped many choices
(prefer prebuilt wheels and ONNX over source builds).

---

## 3. The two processes

Jarvis runs as **two separate programs** that talk over local HTTP:

```
run_friday.ps1
   │
   ├─ 1) starts the TOOL SERVER  ──▶  uv run python -m friday.mcp_server
   │        (FastMCP @ http://127.0.0.1:8765/mcp/)
   │        waits until port 8765 is listening
   │
   └─ 2) starts the VOICE APP    ──▶  uv run python -m friday.main
            (Pipecat pipeline: mic → STT → LLM → TTS → speakers)
            connects to the tool server as an MCP client
```

Why two processes?

- The **voice app** ([friday/main.py](../friday/main.py)) is the real-time audio loop.
- The **tool server** ([friday/mcp_server/](../friday/mcp_server/)) exposes computer
  control. Keeping it separate means it can be restarted, tested, and locked down
  independently — and the voice app **degrades gracefully**: if the tool server is
  down, Jarvis still runs as a plain talk-only assistant (it TCP-checks port 8765
  before connecting, because a failed MCP `start()` would otherwise poison the asyncio
  loop).

The launcher [run_friday.ps1](../run_friday.ps1) ties them together and tears the
server down on exit.

---

## 4. The voice pipeline (the real-time loop)

This is the heart of the app, built entirely in [friday/main.py](../friday/main.py)
as a **Pipecat pipeline**. A pipeline is an ordered list of *frame processors*; audio
and text "frames" flow through them in order. Here is the actual pipeline:

```
transport.input()      # raw mic audio in
  → overlay_reporter    # floating status circle: listening/thinking/working/speaking (services/overlay.py)
  → ptt                 # SPACE push-to-talk  (services/ptt.py)
  → wake                # "hey jarvis" wake word, optional (services/wakeword.py)
  → vision_ptt          # vision hotkey capture (services/vision.py)
  → barge_in            # interrupt an in-progress reply on a new turn (services/barge_in.py)
  → stt                 # faster-whisper transcription
  → TranscriptFilter()  # drop Whisper silence-hallucinations (services/transcript_filter.py)
  → agg.user()          # accumulate the user turn into LLM context
  → vision_injector     # attach a pending screenshot to the turn (services/vision.py)
  → plan_proc           # multi-step commands → plan → execute → summarize, skip LLM (services/plan_processor.py)
  → alias_proc          # single-action commands → execute directly, skip LLM (services/alias_processor.py)
  → llm                 # the brain (decides reply + tool calls)
  → tts                 # Kokoro speech synthesis (services/kokoro_tts.py)
  → transport.output()  # play audio out the speakers
  → agg.assistant()     # record the assistant turn back into context
```

> `plan_proc` and `alias_proc` only join the pipeline when the tool server is up;
> `overlay_reporter` only when `OVERLAY != 0`. They're spliced in conditionally, so a
> talk-only or headless run keeps the minimal chain.

### Turn-taking: who decides when you're "done talking"?

Most voice assistants use Voice Activity Detection (VAD) — they guess when you stopped
speaking. Jarvis instead uses **explicit turn control** so there's no guesswork or
silence-timer lag:

- **Push-to-talk (SPACE):** `PushToTalkProcessor` listens to the keyboard via `pynput`.
  Press → it emits the same "user started speaking" frames a VAD would; release → "user
  stopped speaking". No VAD on this path at all. ([services/ptt.py](../friday/services/ptt.py))

- **Wake word ("hey jarvis"):** `WakeWordProcessor` sits *after* PTT, taps the audio
  frames already flowing by, and feeds 80 ms chunks to a tiny openWakeWord ONNX model.
  On detection it opens the turn; then it hands **turn-ending** to **Silero VAD** —
  when you go quiet after speaking, it closes the turn. It stays dormant while SPACE
  owns a turn (no double-trigger) and has a 1.5 s cooldown to stop repeat firings.
  ([services/wakeword.py](../friday/services/wakeword.py))

### Filtering Whisper's hallucinations

When fed near-silent audio (which the always-listening wake word produces), Whisper
invents stock phrases like *"Thank you."* or *"Thanks for watching."*. `TranscriptFilter`
sits between STT and the context aggregator and silently drops these so they never
become fake conversation turns.
([services/transcript_filter.py](../friday/services/transcript_filter.py))

### Skipping the LLM on the hot path (the two fast-path processors)

The LLM is the slowest hop in a turn. For two large classes of command it can be skipped
entirely. Both processors sit **right before `llm`** and watch the `LLMContextFrame` as it
passes; on a match they execute directly, emit the spoken reply as
`LLMFullResponseStart → TextFrame → LLMFullResponseEnd`, and **do not forward the context
frame** (so the LLM never runs). On *any* miss or error they forward the frame unchanged
and the LLM handles it. Order matters: `plan_proc` runs before `alias_proc` so multi-step
commands are claimed first.

- **`alias_proc`** ([services/alias_processor.py](../friday/services/alias_processor.py)) —
  a table of ~10 regex patterns for common single actions ("set volume to 70", "mute",
  "pause", "next track", "lock the screen", "set brightness to 40", "what's the time").
  Each maps to one MCP tool call (or, for the time, a pure-Python answer with no tool). It
  refuses anything that looks multi-step (an "and then" / "after that" guard) so it can't
  half-handle a compound command. Saves the full ~2–3 s LLM round trip on these.

- **`plan_proc`** ([services/plan_processor.py](../friday/services/plan_processor.py)) —
  fires only on multi-step phrasing ("…and then…", "…after that…"). It makes **one small,
  focused LLM call** (a tiny planning prompt → JSON array of `{tool, args, say}` steps),
  then executes each step over MCP in order and speaks a built-from-results summary
  ("All done." / "Done, but I couldn't …"). This is faster than the default agentic path
  for compound tasks because the planning prompt is tiny and there's **no second LLM call
  to summarize**. JSON parsing tolerates prose wrapping and ```` ``` ```` fences; a bad
  plan falls through to the normal LLM.

> This is the OpenDroid plan-and-alias idea adapted to Jarvis's latency-first design: the
> alias layer is a pure latency win, and the plan layer trades one cheap LLM call for the
> ability to run compound commands without a full tool-calling loop.

### The status overlay

`overlay_reporter` ([services/overlay.py](../friday/services/overlay.py)) drives a small
always-on-top **floating circle** that recolors with Jarvis's state: grey idle → green
**listening** → blue **thinking** → purple **working** (a tool/plan is executing) → orange
**speaking**. It taps the frames already flowing by (`UserStartedSpeaking`,
`FunctionCallsStartedFrame`, `BotStartedSpeaking`, …) and calls `set_state()`. The Tk window
runs in its **own daemon thread**, so repaints never touch the audio loop — state changes
are a lock-guarded enum write, adding no measurable latency. Drag the circle to reposition;
disable with `OVERLAY=0`.

---

## 5. The three local AI models

### STT — faster-whisper
Configured in `main.py` from env: model `large-v3-turbo`, `device=cuda`,
`compute_type=float16`. Transcribes the buffered audio when a turn ends.

### LLM — pluggable brain
The LLM provider is chosen **purely by the `LLM_BASE_URL` env var**:

- URL contains `openrouter` → `OpenRouterLLMService` (cloud, needs `OPENROUTER_API_KEY`).
- Anything else → `OpenAILLMService` pointed at a **local OpenAI-compatible server**
  (LM Studio on `:1234`, Ollama on `:11434`, …).

Both speak the OpenAI API, so **tool/function-calling works identically** either way.
The current default is local **`google/gemma-4-12b-qat`** via LM Studio.

> **Latency gotcha baked into the code:** Gemma reasons by default and returns empty
> replies, burning the token budget on "thinking". The fix is
> `params=OpenAILLMService.InputParams(extra={"reasoning_effort": "none"})` — the one
> switch that disables it. (An earlier Qwen3 model used a `/no_think` prompt prefix for
> the same reason.) Low latency is the project's overriding priority, so these
> reasoning kill-switches matter.

The **system prompt** (in `main.py`) keeps replies to one short spoken sentence, forbids
markdown/emoji (it's read aloud), explains the confirm-gate protocol, and tells the
model to fall back to `run_powershell` for any task without a dedicated tool.

### TTS — Kokoro
`KokoroTTSService` wraps the Kokoro ONNX model as a Pipecat TTS service. It runs on the
GPU via `onnxruntime` CUDA, outputs 24 kHz (matching the audio output, so no
resampling), and falls back to CPU if CUDA can't load. Synthesis runs off the event
loop (`asyncio.to_thread`) so it never blocks the pipeline.
([services/kokoro_tts.py](../friday/services/kokoro_tts.py))

### The GPU-without-CUDA-Toolkit trick (important)

`onnxruntime-gpu` needs CUDA 12 + cuDNN 9 DLLs, but the CUDA Toolkit is **not installed**
on the machine. The workaround: the PyTorch `cu121` wheels bundle exactly those DLLs.
So before importing `onnxruntime`, the code does:

```python
import torch
os.add_dll_directory(os.path.join(os.path.dirname(torch.__file__), "lib"))
```

That puts torch's bundled `cudnn64_9.dll`, `cublasLt64_12.dll`, etc. on the DLL search
path. This is why `pyproject.toml` pins `torch==2.5.1` (+ matching torchaudio/vision) to
the **`pytorch-cu121` wheel index** — those wheels are the CUDA runtime.

---

## 6. The tool server (computer control)

[friday/mcp_server/](../friday/mcp_server/) is a standalone **FastMCP** server. One
instance, all tools registered flat, served over HTTP at
`http://127.0.0.1:8765/mcp/`.

- **Loopback only** (`127.0.0.1`) — never `0.0.0.0` — because `shell.py` exposes a full
  PowerShell. Port 8765 (8000 was taken by other local dev servers); override with
  `FRIDAY_MCP_PORT`.
- The voice app connects with Pipecat's `MCPClient` and calls `register_tools(llm)`,
  which makes every tool callable by the model. Noisy tool outputs (`run_powershell`,
  `search_files`, `list_windows`, …) are truncated to 2000 chars before reaching the
  LLM context.

### The 39 tools, by module

| Module | Tools | Tech |
|---|---|---|
| [apps.py](../friday/mcp_server/apps.py) | `open_app`, `open_url`, `web_search`, `close_app` | friendly-name launch via `cmd /c start` |
| [system.py](../friday/mcp_server/system.py) | `set_volume`, `toggle_mute`, `set_brightness`, `media_control`, `lock_screen`, `sleep_pc`, `power_action` | `pycaw` (audio), media keys, power APIs |
| [sysinfo.py](../friday/mcp_server/sysinfo.py) | `system_info`, `get_context` | `psutil` (battery/CPU/RAM) |
| [windows.py](../friday/mcp_server/windows.py) | `get_active_window`, `list_windows`, `focus_window`, `minimize_window`, `maximize_window` | ctypes/user32 directly |
| [files.py](../friday/mcp_server/files.py) | `open_path`, `search_files`, `create_folder`, `delete_path`, `move_path` | stdlib, sandboxed to home dir |
| [shell.py](../friday/mcp_server/shell.py) | `run_powershell` | the "do anything" escape hatch (gated, 120 s timeout) |
| [input.py](../friday/mcp_server/input.py) | `type_text`, `press_hotkey` | `pynput` keyboard |
| [uia.py](../friday/mcp_server/uia.py) | `click_element`, `list_elements` | `pywinauto` UI Automation — click by **name**, not coordinates |
| [browser.py](../friday/mcp_server/browser.py) | `browser_open`, `browser_read`, `browser_click`, `browser_fill`, `browser_close` | **Playwright**, one persistent Chrome |
| [scheduler.py](../friday/mcp_server/scheduler.py) | `schedule_task`, `list_scheduled_tasks`, `delete_scheduled_task` | native `schtasks.exe`, `Friday_` name prefix |
| [spotify.py](../friday/mcp_server/spotify.py) | `spotify_play` | `spotipy` Web API |
| [safety.py](../friday/mcp_server/safety.py) | `confirm_action`, `cancel_action` | the confirmation gate (below) |

> **Design rule (from project memory):** *always prefer a real API/library call over GUI
> clicking.* `pywinauto` clicks **by element name** (using the UIA Invoke pattern, so
> often no mouse movement at all); OpenCV/coordinate clicking was deliberately avoided.

### The confirmation gate (safety)

Destructive actions (delete, power off, dangerous hotkeys, raw shell) never run
immediately. Instead the tool **stages** a single pending action and returns a
`CONFIRM_REQUIRED [id=xxxxxx]: <summary>` string. The model reads that aloud and asks
you to confirm. Only when `confirm_action(id)` is called with the matching id — within a
**90-second expiry** — does the action actually execute. The gate lives entirely
server-side and is independent of the LLM: nothing destructive runs without that
explicit, id-targeted second call. ([safety.py](../friday/mcp_server/safety.py))

A few capabilities are also constrained at the source: file operations are sandboxed to
the home directory, and the server binds to loopback only.

---

## 7. Screen vision

Hold the **vision key** (default **F9**, configurable via `VISION_KEY`), ask your
question, release. On press the mic opens immediately and, in the background, the screen
is captured with Pillow `ImageGrab` → ~1024 px JPEG (q70) → a `data:` URL. That image is
attached to *this turn's* user message via `LLMContext.create_image_url_message`, and a
vision-capable LLM answers in one call.

This is done **in the Pipecat layer, not via MCP** — the MCP tool layer can't pass
images. The relevant processors:

- `VisionPTTProcessor` (before STT) — handles the hotkey + background capture.
- `VisionContextInjector` (after the user aggregator) — attaches the pending image when
  it sees the `LLMContextFrame`.

([services/vision.py](../friday/services/vision.py),
[services/vision_capture.py](../friday/services/vision_capture.py))

> F9 is the default because "Right Ctrl" reports as `ctrl_l` on this keyboard and
> collides with common shortcuts.

---

## 8. Configuration (`friday/.env`)

All secrets and tunables live in [friday/.env](../friday/.env) (gitignored), loaded by
`python-dotenv`. Key variables:

```ini
# LLM brain — switch provider just by changing the base URL
LLM_BASE_URL=http://localhost:1234/v1     # LM Studio (local). Use openrouter URL for cloud.
LLM_MODEL=google/gemma-4-12b-qat
LLM_API_KEY=local
OPENROUTER_API_KEY=...                     # only used if base URL contains "openrouter"

# STT (faster-whisper)
WHISPER_MODEL=large-v3-turbo
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16

# Tool server
MCP_URL=http://127.0.0.1:8765/mcp/

# Wake word (optional)
WAKE_WORD=1                                # 0 to disable
WAKE_MODEL=hey_jarvis
WAKE_THRESHOLD=0.3

# Vision capture hotkey
VISION_KEY=f9

# UI / behavior toggles
OVERLAY=1                                  # 0 to hide the floating status circle
BARGE_IN=1                                 # 0 to disable interrupting a reply mid-speech

# Spotify Web API (Premium required; redirect must be loopback IP, not "localhost")
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

---

## 9. Running it (0 → 1)

```powershell
# 1. Install the environment (Python 3.11, all deps pinned in pyproject.toml + uv.lock)
uv sync

# 2. Make sure your local LLM is up (e.g. LM Studio serving on :1234),
#    or point LLM_BASE_URL at OpenRouter.

# 3. (one-time, optional) authorize Spotify
uv run python -m friday.auth_spotify

# 4. Launch everything (tool server + voice app)
./run_friday.ps1
```

Then: **hold SPACE** and speak, or say **"hey jarvis"**, or hold **F9** to ask about
your screen. `Ctrl+C` to quit.

You can also run the two halves manually:

```powershell
uv run python -m friday.mcp_server   # tool server only
uv run python -m friday.main         # voice app only (degrades to talk-only if server down)
```

### Tests

```powershell
uv run pytest         # ~100+ tests across friday/mcp_server/tests and friday/tests
```

---

## 10. Repository map

```
jarrrrr/
├─ run_friday.ps1            # launcher: start tool server, wait for port, start voice app
├─ pyproject.toml            # deps, GPU wheel index, pytest config
├─ friday/
│  ├─ main.py                # the Pipecat voice pipeline (entry point)
│  ├─ .env                   # config + secrets (gitignored)
│  ├─ bin/                   # Kokoro ONNX model + voices
│  ├─ auth_spotify.py        # one-time Spotify OAuth → cached refresh token
│  ├─ services/              # Pipecat-side processors (the real-time loop)
│  │  ├─ ptt.py              # SPACE push-to-talk
│  │  ├─ wakeword.py         # "hey jarvis" + Silero VAD turn-end
│  │  ├─ barge_in.py         # interrupt in-progress reply on a new turn
│  │  ├─ kokoro_tts.py       # Kokoro TTS service (GPU)
│  │  ├─ concurrent_llm.py   # batch-parallel tool scheduler (Concurrent*LLMService)
│  │  ├─ mcp_client.py       # MCPClient subclass that surfaces tool errors to the LLM
│  │  ├─ alias_processor.py  # fast-path single-action commands (skip LLM)
│  │  ├─ plan_processor.py   # multi-step commands → plan → execute (skip LLM)
│  │  ├─ overlay.py          # floating status circle (state-colored)
│  │  ├─ vision.py           # screen-vision processors
│  │  ├─ vision_capture.py   # screenshot → data URL
│  │  └─ transcript_filter.py# drop Whisper silence-hallucinations
│  ├─ mcp_server/            # the tool server (computer control)
│  │  ├─ __main__.py         # FastMCP app, registers all 39 tools, serves HTTP
│  │  ├─ apps / system / sysinfo / windows / files / shell / input /
│  │  │  uia / browser / scheduler / spotify .py   # tool modules
│  │  ├─ safety.py           # confirm/cancel gate for destructive actions
│  │  └─ tests/              # per-module tests
│  ├─ tests/                 # service-side tests (ptt, wakeword, vision, filter)
│  └─ tts_server/CosyVoice/  # PARKED: v2 cloned-voice TTS (not used by v1)
└─ docs/
   ├─ ARCHITECTURE.md        # (this file)
   └─ superpowers/{specs,plans}/  # design specs + implementation plans
```

---

## 11. Design principles (the "why")

These thread through every decision in the codebase:

1. **Latency above all.** Local GPU inference, no VAD silence timers on the SPACE path,
   reasoning kill-switches on the LLM, no resampling on the TTS happy path.
2. **API over GUI.** Every capability is a real library/API call. Blind coordinate
   clicking and screen-scraping were deliberately rejected.
3. **Local first, cloud optional.** STT, TTS, and the LLM all run on-device; the cloud
   LLM is a one-env-var fallback.
4. **Minimal and surgical.** Small focused modules; the "do anything" need is met by a
   single gated `run_powershell` rather than a sprawl of niche tools.
5. **Safe by construction.** Loopback-only server, home-sandboxed file ops, and a
   server-side confirmation gate that the LLM cannot bypass.
6. **Graceful degradation.** No tool server? Still a voice assistant. No GPU? Falls back
   to CPU. No image? Vision turn proceeds without it.

---

### Appendix: parked / future work

- **v2 cloned "Jarvis" voice** via CosyVoice3 0.5B lives parked at
  `friday/tts_server/CosyVoice/` in a separate conda env. Swapping v1→v2 is "one TTS
  service swap". It is **not** part of the running v1 app.
- A custom-trained "Friday"/"Jarvis" wake word (vs the pretrained `hey_jarvis`/`alexa`
  models) is deferred — it needs model training.
```
