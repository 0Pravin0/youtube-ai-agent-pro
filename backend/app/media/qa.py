"""Automated quality assurance with transparent, rule-based scoring.

Each check has a weight and a severity. CRITICAL failures block publishing regardless of score.
score = 100 * sum(weight of passed checks) / sum(all weights)."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from app.media import ff


@dataclass
class Check:
    name: str
    passed: bool
    severity: str  # CRITICAL | MAJOR | MINOR
    weight: float
    detail: str
    repair: str | None = None  # suggested targeted repair action

    def as_dict(self):
        return asdict(self)


@dataclass
class QaReport:
    passed: bool
    score: float
    checks: list[Check]

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def as_dict(self):
        return {"passed": self.passed, "score": self.score, "checks": [c.as_dict() for c in self.checks]}


def _black_intervals(path: Path) -> list[tuple[float, float]]:
    err = ff.analyze(["-i", str(path), "-vf", "blackdetect=d=0.25:pix_th=0.08", "-an", "-f", "null", "-"])
    return [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)", err)]


def _silences(path: Path, noise_db: int = -45, min_d: float = 1.5) -> list[tuple[float, float]]:
    err = ff.analyze(["-i", str(path), "-af", f"silencedetect=noise={noise_db}dB:d={min_d}", "-vn", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    out = []
    for i, s in enumerate(starts):
        out.append((s, ends[i] if i < len(ends) else float("inf")))
    return out


def _decode_errors(path: Path) -> str:
    p = ff.sh([ff.ffmpeg(), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-"], 3 * 3600)
    return (p.stderr or "").strip() if p.returncode == 0 else f"exit {p.returncode}: {p.stderr[-500:]}"


def technical_checks(path: Path, *, width: int, height: int, min_s: float, max_s: float,
                     expected_s: float | None, expected_scenes: int, rendered_scenes: int,
                     speech_windows: list[tuple[float, float]], allowed_black: list[tuple[float, float]]) -> list[Check]:
    C: list[Check] = []
    exists = path.exists() and path.stat().st_size > 10_000
    C.append(Check("file_exists", exists, "CRITICAL", 10, f"{path.name} size={path.stat().st_size if path.exists() else 0}",
                   "rerender"))
    if not exists:
        return C
    try:
        info = ff.probe(path)
        C.append(Check("file_readable", True, "CRITICAL", 10, "ffprobe ok"))
    except ff.FFmpegError as e:
        C.append(Check("file_readable", False, "CRITICAL", 10, str(e)[:300], "rerender"))
        return C
    v, a = ff.video_stream(info), ff.audio_stream(info)
    dur = float(info["format"].get("duration", 0))
    C.append(Check("video_codec_h264", bool(v and v["codec_name"] == "h264"), "CRITICAL", 5,
                   f"video={v and v['codec_name']}", "rerender"))
    C.append(Check("audio_codec_aac", bool(a and a["codec_name"] == "aac"), "CRITICAL", 5,
                   f"audio={a and a['codec_name']}", "rerender"))
    res_ok = bool(v and int(v["width"]) == width and int(v["height"]) == height)
    C.append(Check("resolution_aspect", res_ok, "CRITICAL", 8,
                   f"{v and v['width']}x{v and v['height']} expected {width}x{height}", "rerender"))
    C.append(Check("pixel_format", bool(v and v.get("pix_fmt") == "yuv420p"), "MAJOR", 2, f"{v and v.get('pix_fmt')}"))
    C.append(Check("duration_in_range", min_s <= dur <= max_s, "CRITICAL", 8,
                   f"{dur:.2f}s allowed [{min_s}, {max_s}]", "adjust_script_length"))
    if expected_s:
        C.append(Check("duration_matches_timeline", abs(dur - expected_s) <= max(0.5, expected_s * 0.02), "CRITICAL", 6,
                       f"{dur:.2f}s vs planned {expected_s:.2f}s", "rerender"))
    if v and a:
        vd, ad = float(v.get("duration", dur)), float(a.get("duration", dur))
        C.append(Check("av_stream_alignment", abs(vd - ad) < 0.3, "MAJOR", 4, f"video {vd:.2f}s audio {ad:.2f}s", "rerender"))
    C.append(Check("all_scenes_present", rendered_scenes == expected_scenes, "CRITICAL", 8,
                   f"rendered {rendered_scenes}/{expected_scenes} scenes", "regenerate_missing_scenes"))
    # black frames outside allowed transition/card windows
    blacks = _black_intervals(path)

    def allowed(b):
        return any(b[0] >= s - 0.6 and b[1] <= e + 0.6 for s, e in allowed_black)
    bad_black = [b for b in blacks if not allowed(b)]
    C.append(Check("no_unexpected_black", not bad_black, "MAJOR", 6,
                   f"unexpected black intervals: {bad_black[:5]}" if bad_black else "none", "regenerate_scene_visual"))
    # dialogue windows must not be silent
    sil = _silences(path)
    muted = []
    for s, e in speech_windows:
        for a0, a1 in sil:
            overlap = min(e, a1) - max(s, a0)
            if overlap > 0.6 * (e - s) and (e - s) > 0.4:
                muted.append((round(s, 2), round(e, 2)))
                break
    C.append(Check("dialogue_not_silent", not muted, "CRITICAL", 10,
                   f"silent dialogue windows: {muted[:5]}" if muted else f"{len(speech_windows)} dialogue windows audible",
                   "regenerate_voice"))
    long_sil = [x for x in sil if (x[1] - x[0]) > 4.0 and x[1] != float("inf")]
    C.append(Check("no_long_dead_air", not long_sil, "MINOR", 2, f"silences>4s: {long_sil[:3]}"))
    errs = _decode_errors(path)
    C.append(Check("full_decode_clean", not errs, "CRITICAL", 8, errs[:300] or "decoded without errors", "rerender"))
    return C


def content_checks(*, language_ok: bool, language_detail: str, review: dict | None,
                   title: str, captions_present: bool, music_licensed: bool,
                   originality_similarity: float) -> list[Check]:
    C = [
        Check("tamil_language_valid", language_ok, "MAJOR", 6, language_detail, "rewrite_script_lines"),
        Check("captions_present", captions_present, "MAJOR", 4, "subtitles burned + SRT" if captions_present else "missing"),
        Check("music_licensed", music_licensed, "CRITICAL", 8,
              "only owner-registered licensed tracks or original generated audio" if music_licensed else "unlicensed audio source"),
        Check("originality_vs_channel_history", originality_similarity < 0.6, "MAJOR", 5,
              f"max similarity to previous stories = {originality_similarity:.2f}", "rewrite_story"),
        Check("title_length", 0 < len(title) <= 100, "CRITICAL", 3, f"{len(title)} chars"),
    ]
    if review is None:
        C.append(Check("ai_content_review", False, "CRITICAL", 10, "content review unavailable — cannot publish unreviewed"))
    else:
        C += [
            Check("story_consistency", bool(review.get("story_consistent")), "MAJOR", 5, review.get("notes", "")[:300]),
            Check("original_story", bool(review.get("originality_ok")), "CRITICAL", 6, "LLM originality review"),
            Check("title_not_misleading", not review.get("title_misleading"), "CRITICAL", 5, "title vs content"),
            Check("no_rights_concerns", not review.get("rights_concerns"), "CRITICAL", 8, "; ".join(review.get("rights_concerns", []))[:300] or "none"),
            Check("no_policy_concerns", not review.get("policy_concerns"), "CRITICAL", 10, "; ".join(review.get("policy_concerns", []))[:300] or "none"),
            Check("language_quality", int(review.get("language_quality", 0)) >= 6, "MAJOR", 4,
                  f"LLM rating {review.get('language_quality')}/10", "rewrite_script_lines"),
        ]
    return C


def originality_checks(*, near_duplicate_scenes: int, total_scenes: int, same_style_as_last: bool,
                       animation_label: str, layered_scenes: int, camera_scenes: int) -> list[Check]:
    """Monetization-readiness guards: no recycled visuals, no identical template run after run, and an
    honest description of how the video was animated."""
    frac = near_duplicate_scenes / max(1, total_scenes)
    return [
        Check("visuals_not_reused", frac < 0.34, "CRITICAL", 6,
              f"{near_duplicate_scenes}/{total_scenes} scene images are near-identical to images in earlier videos",
              "regenerate_scene_visual"),
        Check("style_varies_from_last_video", not same_style_as_last, "MINOR", 2,
              "edit style (transitions, grade, camera set, subtitle animation) differs from the previous video"
              if not same_style_as_last else "same edit style as the previous video"),
        Check("animation_described_honestly", bool(animation_label), "CRITICAL", 3,
              f"{animation_label} ({layered_scenes} layered-animation scenes, {camera_scenes} camera-motion scenes)"),
    ]


def score(checks: list[Check], min_score: float) -> QaReport:
    total = sum(c.weight for c in checks) or 1
    got = sum(c.weight for c in checks if c.passed)
    s = round(100 * got / total, 1)
    critical_fail = any((not c.passed) and c.severity == "CRITICAL" for c in checks)
    return QaReport(passed=(not critical_fail) and s >= min_score, score=s, checks=checks)


def text_similarity(a: str, b: str) -> float:
    """Character 4-gram Jaccard similarity; cheap originality guard against the channel's own history."""
    def grams(t):
        t = re.sub(r"\s+", " ", t)
        return {t[i:i + 4] for i in range(max(0, len(t) - 3))}
    A, B = grams(a), grams(b)
    return len(A & B) / len(A | B) if A and B else 0.0
