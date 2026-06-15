from pynput import keyboard
from friday.services.ptt import PushToTalkProcessor
from friday.services.vision import VisionState


def test_space_press_clears_pending_image():
    state = VisionState(pending_url="data:image/jpeg;base64,AAA")
    ptt = PushToTalkProcessor(vision_state=state)
    # No event loop set, so _emit is a no-op; we only assert the guard.
    ptt._on_press(keyboard.Key.space)
    assert state.pending_url is None


def test_space_press_without_state_is_safe():
    ptt = PushToTalkProcessor()
    ptt._on_press(keyboard.Key.space)  # must not raise
