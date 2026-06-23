"""
Jarvis floating circle overlay.

A small always-on-top circle that shows Jarvis's current state:
  • IDLE      — small dim dot        (#3a3a3a)
  • LISTENING — large green pulse    (#00e676)
  • THINKING  — large blue pulse     (#1e88e5)
  • WORKING   — large purple pulse   (#ab47bc)  — running a tool / executing a plan
  • SPEAKING  — large orange pulse   (#ff9800)

Runs in its own daemon thread; set_state() is safe to call from any thread / coroutine.
Drag the circle to reposition it. Disable with OVERLAY=0 in .env.
"""

import math
import threading
import tkinter as tk
from enum import Enum, auto

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallsStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class OverlayState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    WORKING = auto()
    SPEAKING = auto()


_COLOR = {
    OverlayState.IDLE: "#3a3a3a",
    OverlayState.LISTENING: "#00e676",  # green
    OverlayState.THINKING: "#1e88e5",   # blue
    OverlayState.WORKING: "#ab47bc",    # purple
    OverlayState.SPEAKING: "#ff9800",   # orange
}

_SIZE = 60    # window px
_PAD_IDLE = 14  # inset from window edge when idle (small circle)
_PAD_ACTIVE = 4  # inset when active (large circle, before pulse)


class JarvisOverlay:
    """Floating always-on-top status circle.

    Call start() once before the pipeline runs.
    Call set_state() from any thread to change the colour/animation.
    """

    def __init__(self, x: int = 40, y: int = 40):
        self._state = OverlayState.IDLE
        self._lock = threading.Lock()
        self._x = x
        self._y = y
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._circle = None
        self._tick = 0

    def set_state(self, state: OverlayState) -> None:
        with self._lock:
            self._state = state

    def start(self) -> None:
        t = threading.Thread(target=self._run_tk, daemon=True, name="jarvis-overlay")
        t.start()

    # ------------------------------------------------------------------ internal

    def _run_tk(self) -> None:
        try:
            root = tk.Tk()
            self._root = root
            root.overrideredirect(True)           # no title bar / border
            root.wm_attributes("-topmost", True)
            root.wm_attributes("-transparentcolor", "white")  # white == fully transparent
            root.configure(bg="white")
            root.geometry(f"{_SIZE}x{_SIZE}+{self._x}+{self._y}")
            root.resizable(False, False)

            canvas = tk.Canvas(root, width=_SIZE, height=_SIZE, bg="white", highlightthickness=0)
            canvas.pack()
            self._canvas = canvas

            # Start as small idle dot
            inset = _PAD_IDLE
            self._circle = canvas.create_oval(
                inset, inset, _SIZE - inset, _SIZE - inset,
                fill=_COLOR[OverlayState.IDLE], outline=""
            )

            # Dragging
            self._drag_start = (0, 0)
            canvas.bind("<ButtonPress-1>", self._drag_press)
            canvas.bind("<B1-Motion>", self._drag_move)

            root.after(40, self._animate)  # 25 fps
            root.mainloop()
        except Exception as exc:
            logger.warning(f"[overlay] thread error: {exc}")

    def _drag_press(self, event):
        self._drag_start = (event.x, event.y)

    def _drag_move(self, event):
        dx = event.x - self._drag_start[0]
        dy = event.y - self._drag_start[1]
        nx = self._root.winfo_x() + dx
        ny = self._root.winfo_y() + dy
        self._root.geometry(f"+{nx}+{ny}")

    def _animate(self) -> None:
        if not self._canvas:
            return
        with self._lock:
            state = self._state

        self._tick += 1
        color = _COLOR[state]

        if state == OverlayState.IDLE:
            # Small static dim dot
            inset = _PAD_IDLE
            self._canvas.coords(self._circle, inset, inset, _SIZE - inset, _SIZE - inset)
        else:
            # Breathing pulse: sin wave ±3 px
            pulse = math.sin(self._tick * 0.18) * 3
            inset = max(2, int(_PAD_ACTIVE - pulse))
            self._canvas.coords(self._circle, inset, inset, _SIZE - inset, _SIZE - inset)

        self._canvas.itemconfig(self._circle, fill=color)
        self._root.after(40, self._animate)


class OverlayStateReporter(FrameProcessor):
    """Watches pipeline frames and updates JarvisOverlay state.

    Place at the top of the pipeline (right after transport.input()) so it
    sees both downstream frames (UserStarted/Stopped) and upstream frames
    (BotStarted/Stopped travelling back from TTS).
    """

    def __init__(self, overlay: JarvisOverlay, **kwargs):
        super().__init__(**kwargs)
        self._overlay = overlay

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            self._overlay.set_state(OverlayState.LISTENING)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._overlay.set_state(OverlayState.THINKING)
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._overlay.set_state(OverlayState.WORKING)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._overlay.set_state(OverlayState.SPEAKING)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._overlay.set_state(OverlayState.IDLE)

        await self.push_frame(frame, direction)
