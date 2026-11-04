"""Audio processing: per-line cleanup, scene narration assembly with pauses, background music
mixing with sidechain ducking, loudness normalization (EBU R128) and true-peak limiting."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.media import ff

SR = 48000
TARGET_LUFS = -14.0  # common streaming target
TRUE_PEAK = -1.5


def clean_line(src: Path, dst: Path) -> float:
    """Trim leading/trailing silence, convert to 48k mono WAV. Returns duration."""
    ff.run(["-i", str(src), "-af",
            "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,"
            "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.08,areverse",
            "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    return ff.duration(dst)


@dataclass
class TimedLine:
    path: Path
    start: float
    end: float
    text: str
    speaker: str


def assemble_scene_voice(lines: list[tuple[Path, str, str, int]], dst: Path, lead_in: float = 0.35,
                         tail: float = 0.5) -> tuple[float, list[TimedLine]]:
    """lines = [(wav, text, speaker, pause_after_ms)]. Concatenate with silences, return
    total duration and per-line timings (for subtitle sync)."""
    inputs, filters, labels, timed = [], [], [], []
    t = lead_in
    idx = 0
    filters.append(f"anullsrc=r={SR}:cl=mono,atrim=duration={lead_in}[s{idx}]")
    labels.append(f"[s{idx}]")
    for i, (wav, text, speaker, pause_ms) in enumerate(lines):
        d = ff.duration(wav)
        inputs += ["-i", str(wav)]
        labels.append(f"[{i}:a]")
        timed.append(TimedLine(wav, round(t, 3), round(t + d, 3), text, speaker))
        t += d
        gap = max(pause_ms / 1000, 0.05) if i < len(lines) - 1 else tail
        idx += 1
        filters.append(f"anullsrc=r={SR}:cl=mono,atrim=duration={gap:.3f}[s{idx}]")
        labels.append(f"[s{idx}]")
        t += gap
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
    ff.run([*inputs, "-filter_complex", ";".join(filters), "-map", "[out]", "-ar", str(SR), "-ac", "1",
            "-c:a", "pcm_s16le", str(dst)])
    return ff.duration(dst), timed


def concat_wavs(paths: list[Path], dst: Path) -> float:
    inputs = []
    for p in paths:
        inputs += ["-i", str(p)]
    labels = "".join(f"[{i}:a]" for i in range(len(paths)))
    ff.run([*inputs, "-filter_complex", f"{labels}concat=n={len(paths)}:v=0:a=1[o]", "-map", "[o]",
            "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    return ff.duration(dst)


def measure_loudness(path: Path) -> dict:
    err = ff.analyze(["-i", str(path), "-af", f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA=11:print_format=json",
                      "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    return json.loads(m.group(0)) if m else {}


def mix_final(voice: Path, dst: Path, music: Path | None = None, music_gain_db: float = -18.0,
              sfx: list[tuple] | None = None) -> dict:
    """Voice + optional licensed music bed (looped, ducked under narration) + SFX, then
    two-pass loudnorm to -14 LUFS with true-peak ceiling and a final limiter (no clipping)."""
    dur = ff.duration(voice)
    inputs = ["-i", str(voice)]
    chains = ["[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[v][vkey]"]
    mix_labels = ["[v]"]
    n = 1
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        chains.append(f"[{n}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=duration={dur:.3f},"
                      f"volume={music_gain_db}dB,afade=t=in:d=1.5,afade=t=out:st={max(0, dur - 2.5):.3f}:d=2.5[m]")
        chains.append("[m][vkey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400:makeup=1[md]")
        mix_labels.append("[md]")
        n += 1
    else:
        chains[0] = "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[v]"
    for entry in (sfx or []):
        path, at = entry[0], entry[1]
        gain = entry[2] if len(entry) > 2 else -6.0
        inputs += ["-i", str(path)]
        ms = int(at * 1000)
        chains.append(f"[{n}:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay={ms}|{ms},volume={gain}dB[x{n}]")
        mix_labels.append(f"[x{n}]")
        n += 1
    chains.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:normalize=0[mix]")
    pre = dst.with_suffix(".premix.wav")
    ff.run([*inputs, "-filter_complex", ";".join(chains), "-map", "[mix]", "-t", f"{dur:.3f}",
            "-c:a", "pcm_s16le", str(pre)])
    stats = measure_loudness(pre)
    if stats:
        ln = (f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA=11:measured_I={stats['input_i']}:"
              f"measured_TP={stats['input_tp']}:measured_LRA={stats['input_lra']}:"
              f"measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:linear=true")
    else:
        ln = f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA=11"
    ff.run(["-i", str(pre), "-af", f"{ln},alimiter=limit=0.84:level=false,aresample=48000",
            "-c:a", "pcm_s16le", str(dst)])
    pre.unlink(missing_ok=True)
    final = measure_loudness(dst)
    return {"input": stats, "output": final, "music": str(music) if music else None}
