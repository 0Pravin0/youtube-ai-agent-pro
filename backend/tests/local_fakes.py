"""TEST DOUBLES for the free local engines — real HTTP servers / a real worker process speaking the same
protocols as Ollama (/api/chat, /api/tags, /api/pull), ComfyUI (/prompt, /history, /view, /upload/image,
/system_stats, /object_info) and the Parler-TTS worker (JSON lines). They let the real app code (clients,
orchestrator, renderer, QA) run end to end without downloading multi-GB models."""
from __future__ import annotations

import io
import json
import math
import os
import random
import stat
import struct
import sys
import threading
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw


class _Server:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.daemon_threads = True
        self.httpd.fake = self
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    @property
    def fake(self):
        return self.server.fake

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> bytes:
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n) if n else b""


# ------------------------------------------------------------------ Ollama
class FakeOllama(_Server):
    def __init__(self, models=("gemma4:e2b-it-qat",)):
        self.models = list(models)
        self.chats: list[dict] = []
        self.invalid_first = 0  # return this many invalid JSON answers first (repair path)
        super().__init__(_OllamaH)


class _OllamaH(_H):
    def do_GET(self):
        if self.path == "/api/version":
            return self._json(200, {"version": "0.30.9-fake"})
        if self.path == "/api/tags":
            return self._json(200, {"models": [{"name": m, "size": 4_300_000_000} for m in self.fake.models]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        req = json.loads(self._body() or b"{}")
        if self.path == "/api/pull":
            self.send_response(200)
            self.send_header("content-type", "application/x-ndjson")
            self.end_headers()
            total = 1000
            for done in (0, 400, 1000):
                self.wfile.write((json.dumps({"status": "pulling abc", "digest": "sha256:abc", "total": total, "completed": done}) + "\n").encode())
            self.wfile.write(b'{"status":"verifying sha256 digest"}\n{"status":"success"}\n')
            self.fake.models.append(req["model"])
            return
        if self.path != "/api/chat":
            return self._json(404, {"error": "not found"})
        self.fake.chats.append(req)
        if req["model"] not in self.fake.models:
            return self._json(404, {"error": f"model '{req['model']}' not found"})
        content = self._answer(req)
        self.send_response(200)
        self.send_header("content-type", "application/x-ndjson")
        self.end_headers()
        for i in range(0, len(content), 400):  # stream in pieces like the real server
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": content[i:i + 400]}, "done": False},
                                         ensure_ascii=False) + "\n").encode())
        self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                                      "prompt_eval_count": 1200, "eval_count": max(1, len(content) // 3)}) + "\n").encode())

    def _answer(self, req) -> str:
        fmt = req.get("format")
        if not fmt:
            return "ஒரு காலத்தில் ஒரு அழகான கிராமத்தில் மீனா என்ற சிறுமி வாழ்ந்தாள்."
        if self.fake.invalid_first > 0:
            self.fake.invalid_first -= 1
            return '{"oops": true'
        from app import schemas
        from tests.fakes import build_object
        schema = getattr(schemas, fmt.get("title", ""), None)
        prompt = "\n".join(m["content"] for m in req["messages"] if m["role"] == "user")
        return build_object(schema, prompt).model_dump_json()


# ------------------------------------------------------------------ ComfyUI
class FakeComfyUI(_Server):
    def __init__(self, checkpoints=("v1-5-pruned-emaonly-fp16.safetensors",), loras=("lcm-lora-sdv1-5.safetensors",)):
        self.checkpoints, self.loras = list(checkpoints), list(loras)
        self.prompts: dict[str, dict] = {}
        self.uploads: dict[str, bytes] = {}
        self.images: dict[str, bytes] = {}
        self.oom = False
        super().__init__(_ComfyH)


class _ComfyH(_H):
    def do_GET(self):
        f = self.fake
        if self.path == "/system_stats":
            return self._json(200, {"system": {"os": "nt", "ram_total": 16 * 2**30}, "devices": [
                {"name": "cpu", "type": "cpu", "vram_total": 0, "vram_free": 0}]})
        if self.path.startswith("/object_info/CheckpointLoaderSimple"):
            return self._json(200, {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": ["COMBO", {"options": f.checkpoints}]}}}})
        if self.path.startswith("/object_info/LoraLoader"):
            return self._json(200, {"LoraLoader": {"input": {"required": {"lora_name": [f.loras, {}]}}}})
        if self.path.startswith("/history/"):
            pid = self.path.split("/")[-1]
            entry = f.prompts.get(pid)
            if not entry:
                return self._json(200, {})
            if f.oom:
                return self._json(200, {pid: {"status": {"status_str": "error", "completed": False, "messages": [
                    ["execution_error", {"exception_message": "CUDA out of memory. Tried to allocate 2 GiB"}]]}, "outputs": {}}})
            return self._json(200, {pid: {"status": {"status_str": "success", "completed": True},
                                          "outputs": {"9": {"images": [{"filename": entry["file"], "subfolder": "", "type": "output"}]}}}})
        if self.path.startswith("/view"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            b = f.images.get(q["filename"][0])
            if b is None:
                return self._json(404, {"error": "no file"})
            self.send_response(200)
            self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        f = self.fake
        if self.path == "/upload/image":
            body = self._body()
            name = f"upload_{uuid.uuid4().hex[:6]}.png"
            f.uploads[name] = body
            return self._json(200, {"name": name, "subfolder": "", "type": "input"})
        if self.path != "/prompt":
            return self._json(404, {"error": "not found"})
        req = json.loads(self._body())
        wf = req["prompt"]
        ck = wf["4"]["inputs"]["ckpt_name"]
        if ck not in f.checkpoints:
            return self._json(400, {"error": {"type": "prompt_outputs_failed_validation"},
                                    "node_errors": {"4": {"errors": [{"message": f"Value not in list: ckpt_name: '{ck}'"}]}}})
        pid = uuid.uuid4().hex
        lat = wf.get("5", {}).get("inputs") or {"width": wf["13"]["inputs"]["width"], "height": wf["13"]["inputs"]["height"]}
        text = wf["6"]["inputs"]["text"]
        fname = f"yta_{len(f.images):05d}_.png"
        f.images[fname] = _picture(text, lat["width"], lat["height"], wf["3"]["inputs"]["seed"])
        f.prompts[pid] = {"workflow": wf, "file": fname}
        self._json(200, {"prompt_id": pid, "number": len(f.prompts), "node_errors": {}})


def _picture(prompt: str, w: int, h: int, seed: int) -> bytes:
    rnd = random.Random(f"{prompt}|{seed}")
    key = (0, 255, 0) if "pure green" in prompt else (255, 0, 255) if "pure magenta" in prompt else None
    if key:
        im = Image.new("RGB", (w, h), key)
        d = ImageDraw.Draw(im)
        d.ellipse([w * .37, h * .12, w * .63, h * .27], fill=(225, 180, 140))
        d.rectangle([w * .38, h * .27, w * .62, h * .65], fill=tuple(rnd.randint(90, 200) for _ in range(3)))
        d.rectangle([w * .40, h * .65, w * .48, h * .9], fill=(60, 40, 30))
        d.rectangle([w * .52, h * .65, w * .60, h * .9], fill=(60, 40, 30))
    else:
        im = Image.new("RGB", (w, h), tuple(rnd.randint(20, 120) for _ in range(3)))
        d = ImageDraw.Draw(im)
        for _ in range(30):
            x, y, r = rnd.randint(0, w), rnd.randint(0, h), rnd.randint(10, 120)
            d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(rnd.randint(40, 255) for _ in range(3)))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ TTS worker
WORKER = r'''
import json, math, struct, sys, wave
print(json.dumps({"ready": True, "device": "cpu", "model": "fake-parler", "sample_rate": 24000}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req["cmd"] == "quit":
        print(json.dumps({"id": req["id"], "ok": True}), flush=True); break
    if req["cmd"] == "ping":
        print(json.dumps({"id": req["id"], "ok": True, "device": "cpu"}), flush=True); continue
    text = req["text"]
    letters = len([c for c in text if not c.isspace()])
    secs = 0.2 if "BREAK" in text else max(0.8, letters / 11.0)
    sr = 24000
    n = int(secs * sr)
    f0 = 140 + (req.get("seed", 0) % 7) * 15
    frames = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * f0 * i / sr) * (0.6 + 0.4 * math.sin(2 * math.pi * 3 * i / sr))))
                      for i in range(n))
    with wave.open(req["out"], "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(frames)
    print(json.dumps({"id": req["id"], "ok": True, "seconds": secs, "sample_rate": sr, "wall": 0.01}), flush=True)
'''


def fake_tts_python(dirpath: Path) -> Path:
    """An executable that behaves like `<voice venv>/python -u parler_worker.py` (ignores its arguments)."""
    dirpath.mkdir(parents=True, exist_ok=True)
    script = dirpath / "fake_parler_worker.py"
    script.write_text(WORKER)
    exe = dirpath / "python-fake"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} -u {script}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe
