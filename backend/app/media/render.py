"""FFmpeg production renderer (free animation pipeline: see app.media.animate).

Timeline = [title card] + scenes + [end card]. Every scene visual is rendered to an intermediate
segment whose length equals that scene's narration length (+ transition overlap), so subtitles and
narration stay in sync after the xfade chain. Scenes that only have a still image get an explicit
camera-motion (Ken Burns) treatment and are recorded as STILL_CAMERA_MOTION in the manifest; they
are never labelled as generated animation."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import TAMIL_FONT_BOLD, TAMIL_FONT_REGULAR
from app.enums import MotionKind
from app.media import animate, ff

FONT_DIR = TAMIL_FONT_REGULAR.parent


@dataclass
class ExportProfile:
    name: str
    width: int
    height: int
    fps: int = 30
    crf: int = 19
    preset: str = field(default_factory=lambda: os.environ.get("RENDER_PRESET", "medium"))
    audio_bitrate: str = "192k"
    transition_s: float = 0.5
    title_card_s: float = 0.0
    end_card_s: float = 0.0

    @property
    def vertical(self) -> bool:
        return self.height > self.width


PROFILES = {
    # Shorts: hook immediately -> no title card; brief end card
    "shorts_1080x1920": ExportProfile("shorts_1080x1920", 1080, 1920, title_card_s=0.0, end_card_s=1.5,
                                      transition_s=0.35),
    # Long-form: title card + 6 s end card (room for YouTube end-screen elements, which are added in Studio)
    "landscape_1920x1080": ExportProfile("landscape_1920x1080", 1920, 1080, title_card_s=3.0, end_card_s=6.0),
}


@dataclass
class SceneVisual:
    index: int
    duration: float  # narration-synced duration
    image: Path | None = None
    video: Path | None = None
    motion: str = "zoom_in"  # camera move for stills (see animate.camera_filter)
    background: Path | None = None  # layered scene: background plate
    sprites: list = field(default_factory=list)  # layered scene: list[animate.Sprite]
    grade: str = "soft"
    light: str = "none"
    pan_dir: int = 1

    @property
    def motion_kind(self) -> MotionKind:
        if self.video:
            return MotionKind.GENERATED_VIDEO
        if self.background and self.sprites:
            return MotionKind.LAYERED_ANIMATION
        return MotionKind.STILL_CAMERA_MOTION


@dataclass
class RenderResult:
    path: Path
    duration: float
    manifest: dict
    wall_seconds: float
    log_tail: str


def _x264(profile: ExportProfile) -> list[str]:
    return ["-c:v", "libx264", "-preset", profile.preset, "-crf", str(profile.crf), "-pix_fmt", "yuv420p",
            "-r", str(profile.fps), "-g", str(profile.fps * 2), "-profile:v", "high", "-movflags", "+faststart"]


def _ken_burns(motion: str, frames: int, W: int, H: int, fps: int = 30) -> str:
    n = max(frames - 1, 1)
    z = {"zoom_in": f"1+0.12*on/{n}", "zoom_out": f"1.12-0.12*on/{n}"}.get(motion, "1.10")
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "pan_left":
        cx = f"(iw-iw/zoom)*(1-on/{n})"
    elif motion == "pan_right":
        cx = f"(iw-iw/zoom)*on/{n}"
    # upscale 2x before zoompan to avoid sub-pixel jitter
    return (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
            f"zoompan=z='{z}':x='{cx}':y='{cy}':d={frames}:s={W}x{H}:fps={fps},setsar=1")


def render_scene_segment(sv: SceneVisual, length: float, profile: ExportProfile, out: Path):
    W, H, fps = profile.width, profile.height, profile.fps
    frames = max(1, round(length * fps))
    look = animate.look_filter(sv.grade, sv.light)
    if sv.video:
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},setsar=1,"
              f"tpad=stop_mode=clone:stop_duration={length:.3f},trim=duration={length:.3f},setpts=PTS-STARTPTS")
        ff.run(["-i", str(sv.video), "-an", "-vf", vf, *_x264(profile), "-t", f"{length:.3f}", str(out)])
    elif sv.background and sv.sprites:
        inputs = ["-loop", "1", "-framerate", str(fps), "-i", str(sv.background)]
        for sp in sv.sprites:
            inputs += ["-loop", "1", "-framerate", str(fps), "-i", str(sp.path)]
        fc = animate.layered_filter(W=W, H=H, fps=fps, length=length, sprites=sv.sprites, pan_dir=sv.pan_dir,
                                    grade=sv.grade, light=sv.light)
        ff.run([*inputs, "-filter_complex", fc, "-map", "[v]", "-an", *_x264(profile), "-frames:v", str(frames), str(out)])
    elif sv.image:
        vf = animate.camera_filter(sv.motion, frames, W, H, fps) + f",{look},fps={fps},trim=duration={length:.3f}"
        ff.run(["-loop", "1", "-framerate", str(fps), "-i", str(sv.image), "-an", "-vf", vf,
                *_x264(profile), "-frames:v", str(frames), str(out)])
    else:
        raise ff.FFmpegError(f"scene {sv.index} has neither image nor video")


def render_card(text_lines: list[str], length: float, profile: ExportProfile, out: Path, work: Path,
                bg_image: Path | None = None, tag: str = "card"):
    """Title / end card: Tamil text (HarfBuzz-shaped via drawtext) over blurred key art or gradient."""
    W, H, fps = profile.width, profile.height, profile.fps
    if bg_image:
        src = ["-loop", "1", "-framerate", str(fps), "-i", str(bg_image)]
        base = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=18:2,"
                f"eq=brightness=-0.25,setsar=1")
    else:
        src = ["-f", "lavfi", "-i", f"gradients=s={W}x{H}:c0=0x1a1033:c1=0x0b3d4f:speed=0.01:r={fps}"]
        base = "[0:v]setsar=1"
    size = int(min(W, H) * (0.085 if len(text_lines) == 1 else 0.065))
    draws = []
    for i, line in enumerate(text_lines):
        tf = work / f"{tag}_{i}.txt"
        tf.write_text(line, encoding="utf-8")
        d0 = 0.25 + 0.35 * i  # staggered entrance
        y = (f"(h-text_h)/2+{int((i - (len(text_lines) - 1) / 2) * size * 1.6)}"
             f"+{int(size * 0.6)}*(1-min(max(t-{d0:.2f},0)/0.7,1))")
        font = TAMIL_FONT_BOLD if i == 0 else TAMIL_FONT_REGULAR
        draws.append(f"drawtext=fontfile='{ff.esc_filter_path(font)}':textfile='{ff.esc_filter_path(tf)}':"
                     f"fontsize={size if i == 0 else int(size * 0.7)}:fontcolor=white:borderw=4:bordercolor=black@0.7:"
                     f"x=(w-text_w)/2:y='{y}':alpha='min(max(t-{d0:.2f},0)/0.7,1)'")
    fade = f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0, length - 0.5):.2f}:d=0.5"
    vf = f"{base},{','.join(draws)},{fade},fps={fps}[v]"
    ff.run([*src, "-filter_complex", vf, "-map", "[v]", "-t", f"{length:.3f}", "-an", *_x264(profile), str(out)])


def render_video(*, scenes: list[SceneVisual], program_audio: Path, subtitles_ass: Path | None,
                 profile: ExportProfile, out: Path, work: Path, title_lines: list[str] | None = None,
                 end_lines: list[str] | None = None, key_art: Path | None = None,
                 transitions: list[str] | None = None, style: dict | None = None) -> RenderResult:
    t0 = time.time()
    work.mkdir(parents=True, exist_ok=True)
    T = profile.transition_s
    segs: list[Path] = []
    seg_durs: list[float] = []  # timeline durations (without overlap)
    parts: list[tuple[str, float, Path | None]] = []
    if profile.title_card_s and title_lines:
        parts.append(("title", profile.title_card_s, None))
    for sv in scenes:
        parts.append((f"scene{sv.index}", sv.duration, None))
    if profile.end_card_s and end_lines:
        parts.append(("end", profile.end_card_s, None))

    manifest_scenes = []
    scene_iter = iter(scenes)
    for i, (name, dur, _) in enumerate(parts):
        is_last = i == len(parts) - 1
        length = dur + (0 if is_last else T)  # extend by overlap so xfade keeps sync
        seg = work / f"seg_{i:03d}_{name}.mp4"
        if name == "title":
            render_card(title_lines or [], length, profile, seg, work, key_art, tag="title")
        elif name == "end":
            render_card(end_lines or [], length, profile, seg, work, key_art, tag="end")
        else:
            sv = next(scene_iter)
            render_scene_segment(sv, length, profile, seg)
            manifest_scenes.append({"index": sv.index, "duration": round(sv.duration, 3),
                                    "motion_kind": sv.motion_kind.value,
                                    "camera_motion": sv.motion if sv.motion_kind == MotionKind.STILL_CAMERA_MOTION else None,
                                    "sprites": len(sv.sprites) if sv.motion_kind == MotionKind.LAYERED_ANIMATION else 0,
                                    "grade": sv.grade, "light": sv.light,
                                    "source": str(sv.video or sv.background or sv.image)})
        segs.append(seg)
        seg_durs.append(dur)

    # xfade chain
    inputs = []
    for s in segs:
        inputs += ["-i", str(s)]
    chain, prev, cum = [], "[0:v]", 0.0
    transitions = transitions or ["fade", "smoothleft", "circleopen", "fade", "dissolve", "smoothright"]
    for i in range(1, len(segs)):
        cum += seg_durs[i - 1]
        lbl = f"[x{i}]"
        chain.append(f"{prev}[{i}:v]xfade=transition={transitions[i % len(transitions)]}:duration={T}:offset={cum:.3f}{lbl}")
        prev = lbl
    total = sum(seg_durs)
    sub = ""
    if subtitles_ass:
        sub = f",subtitles=filename='{ff.esc_filter_path(subtitles_ass)}':fontsdir='{ff.esc_filter_path(FONT_DIR)}'"
    if chain:
        chain.append(f"{prev}format=yuv420p{sub}[vout]")
    else:
        chain.append(f"[0:v]format=yuv420p{sub}[vout]")
    audio_idx = len(segs)
    log = ff.run([*inputs, "-i", str(program_audio), "-filter_complex", ";".join(chain),
                  "-map", "[vout]", "-map", f"{audio_idx}:a", *_x264(profile),
                  "-c:a", "aac", "-b:a", profile.audio_bitrate, "-ar", "48000", "-ac", "2",
                  "-t", f"{total:.3f}", str(out)], timeout=6 * 3600)
    dur = ff.duration(out)
    manifest = {"profile": profile.name, "width": profile.width, "height": profile.height, "fps": profile.fps,
                "planned_duration": round(total, 3), "scenes": manifest_scenes,
                "title_card": bool(profile.title_card_s and title_lines), "end_card": bool(profile.end_card_s and end_lines),
                "still_motion_scenes": sum(1 for s in manifest_scenes if s["motion_kind"] == MotionKind.STILL_CAMERA_MOTION.value),
                "layered_animation_scenes": sum(1 for s in manifest_scenes if s["motion_kind"] == MotionKind.LAYERED_ANIMATION.value),
                "style": style or {},
                "animation_label": animation_label(manifest_scenes),
                "generated_video_scenes": sum(1 for s in manifest_scenes if s["motion_kind"] == MotionKind.GENERATED_VIDEO.value)}
    for s in segs:
        s.unlink(missing_ok=True)
    return RenderResult(out, dur, manifest, time.time() - t0, log[-1500:])


def animation_label(manifest_scenes: list[dict]) -> str:
    """Honest description of how the video was animated (shown in the app and in the YouTube description)."""
    kinds = {s["motion_kind"] for s in manifest_scenes}
    if MotionKind.GENERATED_VIDEO.value in kinds:
        return "AI-generated video clips combined with animated illustrations"
    if MotionKind.LAYERED_ANIMATION.value in kinds:
        return "Motion-comic: layered 2D animation of AI-illustrated characters and backgrounds"
    return "Animated-image storytelling: camera motion over AI illustrations"
