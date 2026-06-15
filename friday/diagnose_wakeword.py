"""Standalone wake-word mic test — isolates openWakeWord from the pipecat plumbing.

Run:  uv run python -m friday.diagnose_wakeword
Then say "hey jarvis" a few times. It prints the live score; anything that
spikes toward 1.0 means detection works (and tells us the right threshold).
If the score never moves off ~0.0, the mic/audio path is the problem, not the
threshold.
"""

import sys

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

CHUNK = 1280          # 80ms @ 16kHz, what openWakeWord expects
MODEL = "hey_jarvis"


def main():
    model = Model(wakeword_models=[MODEL], inference_framework="onnx")
    peak = 0.0
    print(f"Listening on default mic @16kHz. Say 'hey jarvis'. Ctrl+C to stop.")
    print(f"Input device: {sd.query_devices(kind='input')['name']}")

    with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=CHUNK) as stream:
        while True:
            audio, overflowed = stream.read(CHUNK)
            score = model.predict(audio.flatten())[MODEL]
            peak = max(peak, score)
            bar = "#" * int(score * 40)
            sys.stdout.write(f"\rscore={score:0.3f} peak={peak:0.3f} {bar:<40}")
            sys.stdout.flush()
            if score >= 0.5:
                print(f"\n  >>> DETECTED 'hey jarvis' (score {score:0.3f})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
