"""Standalone CosyVoice3 zero-shot smoke test. Run with CWD = this CosyVoice repo dir.
Usage: python smoke_tts.py <prompt_wav> <prompt_transcript> <text_to_say> <out_wav>
"""
import sys
sys.path.append("third_party/Matcha-TTS")
import torch
import torchaudio
from cosyvoice.cli.cosyvoice import AutoModel

prompt_wav, prompt_text, tts_text, out_wav = sys.argv[1:5]
print("cuda_available:", torch.cuda.is_available())

m = AutoModel(model_dir="pretrained_models/Fun-CosyVoice3-0.5B", fp16=torch.cuda.is_available())
# CosyVoice3 expects the system-prompt prefix on the reference transcript
prompt_text = "You are a helpful assistant.<|endofprompt|>" + prompt_text

chunks = [j["tts_speech"] for j in m.inference_zero_shot(tts_text, prompt_text, prompt_wav, stream=False)]
audio = torch.cat(chunks, dim=1)
torchaudio.save(out_wav, audio, m.sample_rate)
print("OK wrote", out_wav, "sr", m.sample_rate, "samples", audio.shape[1])
