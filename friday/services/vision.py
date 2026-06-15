"""Friday screen-vision: shared state, key parsing, and image injection.

A vision turn captures the screen on a dedicated hotkey and attaches it to that
turn's user message so the vision model answers about what's on screen.
"""

from dataclasses import dataclass

from pynput import keyboard

from pipecat.processors.aggregators.llm_context import LLMContext


@dataclass
class VisionState:
    """Shared between the vision hotkey processor and the context injector."""

    pending_url: str | None = None


def resolve_vision_key(name: str):
    """Map a VISION_KEY string to a pynput key. Modifier names (ctrl_r, alt_r,
    ...) map to keyboard.Key.<name>; anything else is treated as a character."""
    key = getattr(keyboard.Key, name, None)
    if key is not None:
        return key
    return keyboard.KeyCode.from_char(name)


def inject_pending_image(context: LLMContext, state: VisionState) -> bool:
    """If an image is pending and the last context message is a plain user
    message, rewrite that message to multimodal (text + image) and clear the
    flag. Returns True if an image was injected."""
    if not state.pending_url:
        return False
    messages = context._messages
    if not messages or messages[-1].get("role") != "user":
        return False
    content = messages[-1].get("content")
    text = content if isinstance(content, str) else None
    messages[-1] = LLMContext.create_image_url_message(url=state.pending_url, text=text)
    state.pending_url = None
    return True
