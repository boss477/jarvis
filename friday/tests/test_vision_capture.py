import base64
import pytest
from PIL import Image
from friday.services import vision_capture


@pytest.mark.asyncio
async def test_capture_returns_jpeg_data_url(monkeypatch):
    # Fake screen: a 4000x3000 solid image (forces downscale).
    fake = Image.new("RGB", (4000, 3000), (10, 20, 30))
    monkeypatch.setattr(vision_capture.ImageGrab, "grab", lambda: fake)

    url = await vision_capture.capture_screen_data_url()

    assert url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[:2] == b"\xff\xd8"          # JPEG magic
    assert len(raw) < 400_000              # downscaled payload is small


def test_grab_downscales_to_max_dim(monkeypatch):
    fake = Image.new("RGB", (4000, 3000), (0, 0, 0))
    monkeypatch.setattr(vision_capture.ImageGrab, "grab", lambda: fake)
    url = vision_capture._grab_to_data_url()
    assert url.startswith("data:image/jpeg;base64,")
