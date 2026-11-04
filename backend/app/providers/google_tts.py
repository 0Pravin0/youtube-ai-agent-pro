"""Google Cloud Text-to-Speech (v1 text:synthesize). Tamil (ta-IN) is supported by Chirp 3: HD
voices, named '<locale>-Chirp3-HD-<Voice>' e.g. ta-IN-Chirp3-HD-Charon. Chirp 3 HD supports
`speaking_rate` and `[pause]` markup (in input.markup, not input.text).
Pricing: Chirp 3 HD $30 / 1M chars (first 1M chars/month free); Standard/WaveNet $4 / 1M.
Auth: an API key restricted to the Text-to-Speech API, kept in Windows Credential Manager."""
from __future__ import annotations

import base64
from pathlib import Path

from app import secrets
from app.providers.base import GenOutput, TTSProvider

URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
VOICES_URL = "https://texttospeech.googleapis.com/v1/voices"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Default casting. Voice names follow the documented Chirp 3 HD pattern; each is checked against
# the live /v1/voices list during the provider test and missing voices are reported.
DEFAULT_CASTING = {
    "narrator": "ta-IN-Chirp3-HD-Charon",
    "child_female": "ta-IN-Chirp3-HD-Leda",
    "child_male": "ta-IN-Chirp3-HD-Puck",
    "adult_female": "ta-IN-Chirp3-HD-Kore",
    "adult_male": "ta-IN-Chirp3-HD-Orus",
    "elder_female": "ta-IN-Chirp3-HD-Sulafat",
    "elder_male": "ta-IN-Chirp3-HD-Iapetus",
    "creature": "ta-IN-Chirp3-HD-Fenrir",
}


class GoogleTTSProvider(TTSProvider):
    key = "google_tts"
    display_name = "Google Cloud Text-to-Speech (Tamil Chirp 3 HD)"
    capabilities = {"languages": ["ta-IN"], "voices": "Chirp3-HD", "speaking_rate": [0.25, 2.0],
                    "pause_markup": True, "ssml": "Pre-GA for Chirp 3 HD", "emotion_control": "prompted via punctuation/pace only"}
    default_pricing = {"usd_per_million_chars": 30.0, "free_chars_per_month": 1_000_000}

    def configured(self) -> bool:
        return bool(secrets.get("google_tts_api_key"))

    def missing_requirement(self) -> str:
        return ("Google Cloud Text-to-Speech API key is not set (Setup → AI providers; enable the "
                "Text-to-Speech API in Google Cloud and create an API key restricted to it)")

    def _auth(self) -> tuple[dict, dict]:
        return {}, {"key": secrets.get("google_tts_api_key")}

    def estimate_cost(self, chars: int) -> float:
        # Conservative: ignore the free tier when reserving budget.
        return chars / 1e6 * float(self.pricing["usd_per_million_chars"])

    def list_voices(self, language_code="ta-IN") -> list[str]:
        h, q = self._auth()
        r = self._request("GET", VOICES_URL, headers=h, params={**q, "languageCode": language_code})
        return [v["name"] for v in r.json().get("voices", [])]

    def test(self) -> str:
        names = self.list_voices("ta-IN")
        chirp = [n for n in names if "Chirp3-HD" in n]
        missing = [v for v in DEFAULT_CASTING.values() if v not in names]
        msg = f"{len(names)} ta-IN voices ({len(chirp)} Chirp3-HD)"
        if missing:
            msg += f"; default casting voices not found: {missing}"
        return msg

    def synthesize(self, text, out, *, voice_name, language_code="ta-IN", speaking_rate=1.0, pitch=0.0) -> GenOutput:
        self.require()
        h, q = self._auth()
        uses_markup = "[pause" in text
        inp = {"markup": text} if uses_markup else {"text": text}
        body = {"input": inp, "voice": {"languageCode": language_code, "name": voice_name},
                "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 48000, "speakingRate": speaking_rate}}
        if pitch and "Chirp3" not in voice_name:  # pitch isn't supported on Chirp 3 HD
            body["audioConfig"]["pitch"] = pitch
        r = self._request("POST", URL, headers=h, params=q, json=body)
        out.write_bytes(base64.b64decode(r.json()["audioContent"]))
        return GenOutput(out, "audio/wav", self.estimate_cost(len(text)), meta={"voice": voice_name, "chars": len(text)})
