"""Deterministic Tamil language checks + pronunciation preprocessing for TTS."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

TAMIL_RANGE = (0x0B80, 0x0BFF)
# Dependent vowel signs + virama (pulli) — must follow a consonant
DEPENDENT = set(range(0x0BBE, 0x0BCE)) | {0x0BD7}
TAMIL_CONSONANTS = set(range(0x0B95, 0x0BBA))
GRANTHA_OK = True


def is_tamil_char(ch: str) -> bool:
    return TAMIL_RANGE[0] <= ord(ch) <= TAMIL_RANGE[1]


@dataclass
class LanguageReport:
    ok: bool
    tamil_ratio: float
    issues: list[str] = field(default_factory=list)
    latin_words: list[str] = field(default_factory=list)
    est_seconds: float = 0.0

    def as_dict(self):
        return self.__dict__.copy()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[​﻿]", "", text)  # zero-width space / BOM (keep ZWJ/ZWNJ)
    return re.sub(r"[ \t]+", " ", text).strip()


def orthography_issues(text: str) -> list[str]:
    issues = []
    prev = " "
    for i, ch in enumerate(text):
        cp = ord(ch)
        if cp in DEPENDENT and not (ord(prev) in TAMIL_CONSONANTS or ord(prev) == 0x0BB7 or 0x0B95 <= ord(prev) <= 0x0BB9):
            issues.append(f"orphan vowel sign/pulli U+{cp:04X} at {i}")
        if cp in DEPENDENT and ord(prev) in DEPENDENT:
            issues.append(f"stacked vowel signs at {i}")
        prev = ch
    return issues[:20]


def estimate_speech_seconds(text: str, chars_per_second: float = 11.0) -> float:
    """Rough Tamil narration rate (~11 Unicode letters/sec at speaking_rate 1.0). Only used for
    planning; real durations come from the synthesized audio."""
    letters = sum(1 for c in text if c.isalpha() or unicodedata.category(c).startswith("M"))
    return round(letters / chars_per_second, 2)


def validate_tamil(text: str, *, min_ratio: float = 0.85, allowed_latin: set[str] | None = None) -> LanguageReport:
    text = normalize(text)
    letters = [c for c in text if c.isalpha() or unicodedata.category(c).startswith("M")]
    tamil = [c for c in letters if is_tamil_char(c)]
    ratio = len(tamil) / len(letters) if letters else 0.0
    allowed = {w.lower() for w in (allowed_latin or set())}
    latin = [w for w in re.findall(r"[A-Za-z]{2,}", text) if w.lower() not in allowed]
    issues = orthography_issues(text)
    if ratio < min_ratio:
        issues.append(f"Tamil script ratio {ratio:.2f} below {min_ratio}")
    if re.search(r"[ऀ-ॿఀ-౿ഀ-ൿ]", text):
        issues.append("contains Devanagari/Telugu/Malayalam characters")
    return LanguageReport(ok=not issues, tamil_ratio=round(ratio, 3), issues=issues, latin_words=latin[:30],
                          est_seconds=estimate_speech_seconds(text))


def apply_pronunciations(text: str, overrides: dict[str, str]) -> str:
    """Owner-configurable respellings (e.g. a character name the TTS mispronounces)."""
    for src, dst in sorted(overrides.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(src, dst)
    return text


def subtitle_chunks(text: str, max_chars: int) -> list[str]:
    """Split a line into subtitle-sized chunks on word boundaries."""
    words, chunks, cur = normalize(text).split(" "), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        chunks.append(cur)
    return chunks
