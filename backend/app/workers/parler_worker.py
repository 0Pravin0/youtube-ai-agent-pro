"""Indic Parler-TTS worker (runs inside the separate "voice" Python environment that the app installs:
torch + parler-tts 0.2.3 + soundfile). Speaks JSON lines on stdin/stdout so the model loads once.

Requests:  {"id": 1, "cmd": "synth", "text": "...", "description": "...", "seed": 7, "out": "C:/.../x.wav"}
           {"id": 2, "cmd": "ping"}      {"id": 3, "cmd": "quit"}
Replies:   {"id": 1, "ok": true, "seconds": 3.2, "sample_rate": 44100, "wall": 9.1}
           {"id": 1, "ok": false, "error": "..."}
First line written after loading: {"ready": true, "device": "cuda:0" | "cpu", "model": "..."}"""
import json
import os
import re
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def split_sentences(text, limit=180):
    parts = [p.strip() for p in re.split(r"(?<=[.!?।…])\s+", text.strip()) if p.strip()]
    out = []
    for p in parts:
        while len(p) > limit:  # break overly long sentences at a comma or space
            cut = max(p.rfind(",", 0, limit), p.rfind(" ", 0, limit))
            cut = cut if cut > limit // 3 else limit
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return out or [text]


def main():
    model_id = os.environ.get("YTA_TTS_MODEL", "ai4bharat/indic-parler-tts")
    import numpy as np
    import soundfile as sf
    import torch
    from parler_tts import ParlerTTSForConditionalGeneration
    from transformers import AutoTokenizer

    want = os.environ.get("YTA_TTS_DEVICE", "auto")
    if want == "auto":
        device = "cuda:0" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    else:
        device = want
    dtype = torch.float16 if device.startswith("cuda") else torch.float32  # fp16 on Apple MPS can produce silence
    token = os.environ.get("HF_TOKEN") or None
    model = ParlerTTSForConditionalGeneration.from_pretrained(model_id, token=token, torch_dtype=dtype).to(device)
    tok = AutoTokenizer.from_pretrained(model_id, token=token)
    desc_tok = AutoTokenizer.from_pretrained(os.environ.get("YTA_TTS_DESC_TOKENIZER") or model.config.text_encoder._name_or_path,
                                             token=token)
    sr = model.config.sampling_rate
    emit({"ready": True, "device": device, "model": model_id, "sample_rate": sr})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        rid = req.get("id")
        try:
            if req["cmd"] == "quit":
                emit({"id": rid, "ok": True})
                return
            if req["cmd"] == "ping":
                emit({"id": rid, "ok": True, "device": device})
                continue
            t0 = time.time()
            d = desc_tok(req["description"], return_tensors="pt").to(device)
            chunks = []
            gap = np.zeros(int(sr * 0.12), dtype=np.float32)
            for sentence in split_sentences(req["text"]):
                torch.manual_seed(int(req.get("seed", 0)))  # same speaker sound for the same voice profile
                p = tok(sentence, return_tensors="pt").to(device)
                try:
                    with torch.inference_mode():
                        gen = model.generate(input_ids=d.input_ids, attention_mask=d.attention_mask,
                                             prompt_input_ids=p.input_ids, prompt_attention_mask=p.attention_mask)
                except (RuntimeError, NotImplementedError):
                    if device == "cpu":
                        raise
                    device = "cpu"  # an operation the Apple GPU backend doesn't support: continue on the CPU
                    model = model.to(device)
                    d = desc_tok(req["description"], return_tensors="pt").to(device)
                    p = tok(sentence, return_tensors="pt").to(device)
                    with torch.inference_mode():
                        gen = model.generate(input_ids=d.input_ids, attention_mask=d.attention_mask,
                                             prompt_input_ids=p.input_ids, prompt_attention_mask=p.attention_mask)
                chunks += [gen.to(torch.float32).cpu().numpy().squeeze(), gap]
            audio = np.concatenate(chunks[:-1]) if chunks else np.zeros(1, dtype=np.float32)
            sf.write(req["out"], audio, sr, subtype="PCM_16")
            emit({"id": rid, "ok": True, "seconds": round(len(audio) / sr, 3), "sample_rate": sr,
                  "wall": round(time.time() - t0, 2), "device": device})
        except Exception as e:  # report and keep serving
            emit({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"[:1000]})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        emit({"ready": False, "error": f"{type(e).__name__}: {e}"[:2000]})
        sys.exit(1)
