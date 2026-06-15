"""Capture the primary screen as a small JPEG data URL for the vision model."""

import asyncio
import base64
import io

from PIL import ImageGrab

MAX_DIM = 1024
JPEG_QUALITY = 70


def _grab_to_data_url() -> str:
    """Grab the primary screen, downscale, JPEG-encode, return a data: URL."""
    img = ImageGrab.grab()
    img.thumbnail((MAX_DIM, MAX_DIM))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


async def capture_screen_data_url() -> str:
    """Async wrapper — runs the blocking grab/encode off the event loop."""
    return await asyncio.to_thread(_grab_to_data_url)
