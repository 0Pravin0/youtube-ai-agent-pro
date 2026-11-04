"""ASS subtitle generation with Tamil font and platform safe areas."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.media.tamil import subtitle_chunks


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class SubtitleStyle:
    width: int
    height: int
    font_size: int
    margin_v: int  # distance from bottom; Shorts keeps clear of the bottom UI overlay
    margin_lr: int
    max_chars: int

    @classmethod
    def for_profile(cls, w: int, h: int) -> "SubtitleStyle":
        if h > w:  # 9:16 Shorts: bottom ~20% is covered by title/channel UI, right edge by buttons
            return cls(w, h, font_size=62, margin_v=int(h * 0.24), margin_lr=int(w * 0.11), max_chars=22)
        return cls(w, h, font_size=52, margin_v=int(h * 0.07), margin_lr=int(w * 0.08), max_chars=44)


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def cues_from_lines(lines: list[tuple[float, float, str]], max_chars: int) -> list[Cue]:
    """Split each spoken line into chunks; distribute its time proportionally to chunk length."""
    cues = []
    for start, end, text in lines:
        chunks = subtitle_chunks(text, max_chars)
        # pair chunks into two-line cues
        groups = [chunks[i:i + 2] for i in range(0, len(chunks), 2)]
        total = sum(len("".join(g)) for g in groups) or 1
        t = start
        for g in groups:
            d = (end - start) * len("".join(g)) / total
            cues.append(Cue(t, t + d, "\n".join(g)))
            t += d
    return cues


def _animated(text: str, dur: float, animation: str | None) -> str:
    """ASS override tags: 'pop' = fade + quick scale pop; 'karaoke' = words fill in time with speech."""
    body = _esc(text)
    if animation == "pop":
        return r"{\fad(120,80)\fscx92\fscy92\t(0,160,\fscx104\fscy104)\t(160,300,\fscx100\fscy100)}" + body
    if animation == "karaoke":
        words = text.replace("\n", " \n ").split(" ")
        letters = sum(len(w) for w in words if w != "\n") or 1
        cs_total = max(1, int(dur * 100 * 0.92))
        parts = []
        for w in words:
            if w == "\n":
                parts.append("\\N")
                continue
            if not w:
                continue
            parts.append(f"{{\\kf{max(1, round(cs_total * len(w) / letters))}}}{_esc(w)} ")
        return r"{\fad(100,80)}" + "".join(parts).rstrip()
    return body


def write_ass(cues: list[Cue], style: SubtitleStyle, path: Path, font_name: str = "Noto Sans Tamil",
              animation: str | None = None):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {style.width}
PlayResY: {style.height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{style.font_size},&H00FFFFFF,&H0060D0FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,{style.margin_lr},{style.margin_lr},{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    body = "".join(f"Dialogue: 0,{_ts(c.start)},{_ts(c.end)},Default,,0,0,0,,{_animated(c.text, c.end - c.start, animation)}\n"
                   for c in cues)
    path.write_text(header + body, encoding="utf-8")


def write_srt(cues: list[Cue], path: Path):
    """SRT copy for uploading as a YouTube caption track / accessibility."""
    def ts(t):
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    out = [f"{i}\n{ts(c.start)} --> {ts(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    path.write_text("\n".join(out), encoding="utf-8")
