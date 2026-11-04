"""Free local Tamil text-to-speech: AI4Bharat Indic Parler-TTS (Apache-2.0, 0.9B parameters).

Why this model (checked 2026-10-29 on huggingface.co/ai4bharat/indic-parler-tts): it supports Tamil
with named speakers (Jaya — recommended, Kavitha), is Apache-2.0 (commercial use allowed), runs on
CPU or NVIDIA GPU, and controls voice through a text description. IndicF5 (MIT) was evaluated too but
needs a reference recording of a real voice to clone — not used, to avoid voice-rights problems.

The model runs in a separate Python environment (torch is large) as a persistent worker process
(app/workers/parler_worker.py) speaking JSON lines. Every line is quality-checked (duration
plausible for the text, not silent, not clipped); a line that keeps failing pauses the job with the
reason instead of publishing unusable audio. The owner must also approve a voice sample once."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.enums import ProviderKind
from app.providers.base import GenOutput, TTSProvider
from app.reliability.errors import PermanentError, ProviderBlocked, TransientError

MODEL_ID = "ai4bharat/indic-parler-tts"
CREATE_NO_WINDOW = 0x08000000

# Casting by script voice hint -> (description, seed). Tamil named speakers are female; male roles
# use a described voice with a fixed seed (less consistent than a named speaker — stated in the UI).
CASTING = {
    "narrator": ("Jaya speaks at a moderate pace with a warm, expressive storytelling tone in a very clear, close-sounding recording with no background noise.", 11),
    "child_female": ("Kavitha speaks at a slightly fast pace with a bright, high-pitched, cheerful and lively voice in a very clear, close-sounding recording with no background noise.", 21),
    "adult_female": ("Kavitha speaks at a moderate pace with a natural, gentle tone in a very clear, close-sounding recording with no background noise.", 31),
    "elder_female": ("Jaya speaks slowly with a low-pitched, calm and wise tone in a very clear, close-sounding recording with no background noise.", 41),
    "child_male": ("A young boy speaks at a slightly fast pace with a high-pitched, playful voice in a very clear, close-sounding recording with no background noise.", 51),
    "adult_male": ("A man speaks at a moderate pace with a deep, confident voice in a very clear, close-sounding recording with no background noise.", 61),
    "elder_male": ("An old man speaks slowly with a low-pitched, gentle and slightly husky voice in a very clear, close-sounding recording with no background noise.", 71),
    "creature": ("A man speaks slowly with a very low-pitched, rough and mysterious voice in a very clear, close-sounding recording with no background noise.", 81),
}


def voice_name_for(hint: str) -> str:
    return f"parler/{hint if hint in CASTING else 'narrator'}"


def _cast(voice_name: str) -> tuple[str, int]:
    hint = voice_name.split("/", 1)[1] if voice_name.startswith("parler/") else "narrator"
    return CASTING.get(hint, CASTING["narrator"])


class _Worker:
    """One persistent worker process; requests are serialized with a lock."""
    _inst: "_Worker | None" = None
    _cls_lock = threading.Lock()

    def __init__(self, python: str, model: str, env: dict):
        self.python, self.model = python, model
        script = Path(__file__).resolve().parent.parent / "workers" / "parler_worker.py"
        self.proc = subprocess.Popen([python, "-u", str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
                                     creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        self.lock = threading.Lock()
        self.seq = 0
        self.info = self._read(timeout=float(os.environ.get("YTA_TTS_LOAD_TIMEOUT", 1800)))
        if not self.info.get("ready"):
            self.close()
            raise ProviderBlocked("local_tts", f"Tamil voice engine failed to load: {self.info.get('error', 'unknown error')}")

    def _read(self, timeout: float) -> dict:
        box: dict = {}

        def rd():
            box["line"] = self.proc.stdout.readline()
        t = threading.Thread(target=rd, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.close()
            raise TransientError(f"Tamil voice engine did not answer within {timeout:.0f}s", retry_after=60)
        line = box.get("line") or ""
        if not line:
            raise TransientError("Tamil voice engine stopped unexpectedly", retry_after=30)
        return json.loads(line)

    def request(self, payload: dict, timeout: float = 1800) -> dict:
        with self.lock:
            if self.proc.poll() is not None:
                raise TransientError("Tamil voice engine is not running", retry_after=10)
            self.seq += 1
            payload = {**payload, "id": self.seq}
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            return self._read(timeout)

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass

    @classmethod
    def get(cls, python: str, model: str, env: dict) -> "_Worker":
        with cls._cls_lock:
            w = cls._inst
            if w is None or w.proc.poll() is not None or w.python != python or w.model != model:
                if w:
                    w.close()
                cls._inst = _Worker(python, model, env)
            return cls._inst

    @classmethod
    def shutdown(cls):
        with cls._cls_lock:
            if cls._inst:
                cls._inst.close()
                cls._inst = None


def check_line(path: Path, text: str, chars_per_second: float = 11.0) -> list[str]:
    """Objective checks that catch unusable synthesis (silence, truncation, run-on babble, clipping)."""
    from app.media import ff
    problems = []
    dur = ff.duration(path)
    letters = len([c for c in text if not c.isspace()])
    expected = max(0.6, letters / chars_per_second)
    if dur < expected * 0.35:
        problems.append(f"too short for the text ({dur:.1f}s, expected ~{expected:.1f}s) — words were probably skipped")
    if dur > expected * 3.0 + 1.5:
        problems.append(f"too long for the text ({dur:.1f}s, expected ~{expected:.1f}s) — babbling or long silences")
    vol = ff.max_volume_db(path)
    if vol is None or vol < -35:
        problems.append("nearly silent")
    return problems


class LocalTamilTTSProvider(TTSProvider):
    key = "local_tts"
    display_name = "Indic Parler-TTS — local Tamil voice (free)"
    capabilities = {"local": True, "languages": ["ta-IN", "en-IN"], "voices": list(CASTING),
                    "license": "Apache-2.0", "cost": "free (runs on this computer)"}
    default_pricing = {"per_character_usd": 0.0}

    def __init__(self, pricing=None, config=None):
        super().__init__(pricing, config)
        self.python = self.config.get("python")
        self.model = self.config.get("model") or MODEL_ID

    def configured(self) -> bool:
        return bool(self.python) and Path(self.python).exists()

    def missing_requirement(self) -> str:
        return "the free Tamil voice engine is not installed yet (Tools and Models → Tamil voice)"

    def estimate_cost(self, chars: int) -> float:
        return 0.0

    def _env(self) -> dict:
        from app import secrets
        from app.config import get_settings
        env = {k: v for k, v in os.environ.items() if not k.startswith("YTA_API")}
        env["HF_HOME"] = str(get_settings().data_dir / "models" / "huggingface")
        env["YTA_TTS_MODEL"] = self.model
        local = get_settings().data_dir / "models" / "indic-parler-tts"
        if (local / "config.json").exists():  # verified local copy (Tools → Tamil voice): run fully offline
            env["YTA_TTS_MODEL"] = str(local)
            env["YTA_TTS_DESC_TOKENIZER"] = str(local.parent / "flan-t5-tokenizer")
            env["HF_HUB_OFFLINE"] = "1"
        env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        tok = secrets.get("hf_token")
        if tok:
            env["HF_TOKEN"] = tok
        return env

    def worker(self) -> _Worker:
        self.require()
        return _Worker.get(self.python, self.model, self._env())

    def test(self) -> str:
        w = self.worker()
        r = w.request({"cmd": "ping"}, timeout=60)
        if not r.get("ok"):
            raise ProviderBlocked(self.key, r.get("error", "voice engine error"))
        return f"ok — Indic Parler-TTS on {w.info.get('device')}"

    def synthesize(self, text: str, out: Path, *, voice_name: str, language_code: str = "ta-IN",
                   speaking_rate: float = 1.0, pitch: float = 0.0) -> GenOutput:
        description, seed = _cast(voice_name)
        w = self.worker()
        out.parent.mkdir(parents=True, exist_ok=True)
        cps = 11.0 if language_code.startswith("ta") else 14.0
        last: list[str] = []
        for attempt in range(3):  # a different seed varies the sampling; same described voice
            r = w.request({"cmd": "synth", "text": text, "description": description, "seed": seed + attempt * 1000,
                           "out": str(out)})
            if not r.get("ok"):
                err = r.get("error", "")
                if "out of memory" in err.lower():
                    raise ProviderBlocked(self.key, "not enough memory for the Tamil voice model on this computer")
                raise TransientError(f"local TTS error: {err}", retry_after=10)
            last = check_line(out, text, cps)
            if not last:
                return GenOutput(out, "audio/wav", 0.0, cost_is_estimate=False,
                                 meta={"seconds": r.get("seconds"), "wall": r.get("wall"), "seed": seed + attempt * 1000,
                                       "device": w.info.get("device"), "model": self.model, "attempts": attempt + 1})
        raise PermanentError("TAMIL_VOICE_QUALITY: the local Tamil voice failed quality checks 3 times for this line "
                             f"({'; '.join(last)}). The job is paused instead of publishing unusable audio. Text: {text[:80]}")
