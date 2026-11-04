"""Per-language settings. Tamil is the primary, fully validated language; English is supported for
script, voices (Google Chirp 3 HD en-IN) and metadata, without the Tamil orthography checks."""
from __future__ import annotations

LANGS = {
    "ta": {"name": "Tamil", "native": "தமிழ்", "tts": "ta-IN", "chars_per_second": 11.0,
           "thanks": "நன்றி!", "subscribe": "மேலும் கதைகளுக்கு Subscribe செய்யுங்கள்",
           "writing": "natural, contemporary spoken Tamil (தமிழ்) that a Tamil family would enjoy — clear and "
                      "expressive, not archaic, not English-mixed (use Tamil words; avoid Tanglish). Write numbers as Tamil words.",
           "search_queries": ["தமிழ் கதை", "Tamil animated story"]},
    "en": {"name": "English", "native": "English", "tts": "en-IN", "chars_per_second": 14.0,
           "thanks": "Thanks for watching!", "subscribe": "Subscribe for more stories",
           "writing": "clear, warm, natural spoken English with short sentences that read well aloud.",
           "search_queries": ["animated story for kids", "fantasy animated short"]},
}


def lang(code: str | None) -> dict:
    return LANGS.get(code or "ta", LANGS["ta"])


def voice_name(code: str, tamil_voice: str) -> str:
    """Map a casting voice (defined with the ta-IN prefix) to the channel's locale."""
    return tamil_voice.replace("ta-IN-", f"{lang(code)['tts']}-", 1)
