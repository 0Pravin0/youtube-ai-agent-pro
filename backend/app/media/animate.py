"""Free animation toolkit (FFmpeg + Pillow, no AI video model needed).

What this produces — described honestly everywhere it is shown:
* LAYERED 2.5D ANIMATION ("motion-comic"): a background plate moves with slow parallax while
  cut-out character sprites move independently on top (idle breathing, talking bounce while their
  line is spoken, slide-in entrances), with lighting and colour effects. Characters are animated as
  whole cut-outs — there is no lip-sync or limb animation.
* ANIMATED-IMAGE STORYTELLING: a full illustration with camera moves (zoom, pan, tilt, diagonal).
* GENERATED VIDEO: only when a premium video API is enabled.

Character sprites are generated on a flat key colour (green or magenta, whichever the character's
clothing doesn't use) and keyed out here with spill suppression and a soft edge."""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageFilter

from app.media import ff

# ---------------------------------------------------------------- chroma key
KEYS = {"green": (0, 255, 0), "magenta": (255, 0, 255)}


def key_color_for(description: str) -> str:
    d = description.lower()
    return "magenta" if any(w in d for w in ("green", "emerald", "leaf", "jade", "olive", "lime")) else "green"


def chroma_prompt(key: str) -> str:
    hexes = {"green": "#00FF00", "magenta": "#FF00FF"}
    return (f"full body, standing, whole character visible, centered, isolated on a plain flat solid pure {key} "
            f"({hexes[key]}) background, no ground, no shadow, no scenery")


@dataclass
class KeyResult:
    ok: bool
    coverage: float
    border_keyed: float
    reason: str = ""


def key_out(src: Path, dst: Path, key: str = "green") -> KeyResult:
    """Remove the flat key colour -> RGBA PNG cropped to the character. Rejects images where the
    background wasn't a clean flat colour (then the scene falls back to a full illustration)."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    px = im.load()
    alpha = Image.new("L", (w, h), 255)
    ap = alpha.load()
    out = Image.new("RGBA", (w, h))
    op = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if key == "green":
                dom, o1, o2 = g, r, b
            else:  # magenta: red and blue high, green low
                dom, o1, o2 = min(r, b), g, (abs(r - b))
            spill = dom - max(o1, o2) if key == "green" else dom - o1
            if key == "green" and g > 80 and g > 1.35 * max(r, b):
                a = 0
            elif key == "magenta" and min(r, b) > 80 and min(r, b) > 1.35 * g:
                a = 0
            elif spill > 25:
                a = max(0, min(255, 255 - (spill - 25) * 4))
            else:
                a = 255
            ap[x, y] = a
            if key == "green" and g > max(r, b):  # spill suppression
                g = max(r, b)
            elif key == "magenta" and min(r, b) > g:
                avg = (r + b) // 2
                r = b = min(avg, g + 30)
            op[x, y] = (r, g, b, a)
    border = [ap[x, 0] for x in range(w)] + [ap[x, h - 1] for x in range(w)] + \
             [ap[0, y] for y in range(h)] + [ap[w - 1, y] for y in range(h)]
    border_keyed = sum(1 for a in border if a == 0) / len(border)
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.2))  # erode + feather edge
    out.putalpha(alpha)
    coverage = sum(1 for a in alpha.getdata() if a > 128) / (w * h)
    if border_keyed < 0.85:
        return KeyResult(False, coverage, border_keyed, "background is not a clean flat colour")
    if not 0.04 <= coverage <= 0.75:
        return KeyResult(False, coverage, border_keyed, f"character covers {coverage:.0%} of the frame")
    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst)
    return KeyResult(True, coverage, border_keyed)


# ---------------------------------------------------------------- looks
GRADES = {
    "warm": "colorbalance=rs=0.05:bs=-0.05:rm=0.03:bm=-0.03,eq=saturation=1.08",
    "golden": "colorbalance=rs=0.08:gs=0.03:bs=-0.08:rh=0.05,eq=saturation=1.12:contrast=1.03",
    "moonlight": "colorbalance=rs=-0.06:bs=0.10:bm=0.06,eq=brightness=-0.04:saturation=0.85",
    "vivid": "eq=saturation=1.25:contrast=1.06",
    "soft": "eq=saturation=0.95:contrast=0.96:brightness=0.02",
    "storm": "colorbalance=bs=0.05:bm=0.04,eq=saturation=0.75:contrast=1.08:brightness=-0.05",
}
MOOD_WORDS = {
    "moonlight": ("night", "moon", "dark", "star", "இரவு", "நிலா"),
    "golden": ("sunset", "dawn", "sunrise", "evening", "gold", "festival"),
    "storm": ("storm", "rain", "thunder", "flood", "மழை"),
    "vivid": ("magic", "rainbow", "celebration", "dance", "spark"),
    "warm": ("home", "village", "kitchen", "morning", "fire", "lamp"),
}
FLICKER_WORDS = ("fire", "lamp", "candle", "torch", "campfire", "விளக்கு")


def look_for(text: str, base: str) -> tuple[str, str]:
    t = text.lower()
    grade = next((g for g, ws in MOOD_WORDS.items() if any(w in t for w in ws)), base)
    light = "flicker" if any(w in t for w in FLICKER_WORDS) else "none"
    return grade, light


def look_filter(grade: str, light: str) -> str:
    f = GRADES.get(grade, GRADES["soft"])
    if light == "flicker":
        f += ",eq=brightness='0.025*sin(2*PI*t*1.7)+0.015*sin(2*PI*t*4.3)':eval=frame"
    return f + ",vignette=angle=PI/5"


# ---------------------------------------------------------------- style variety
TRANSITION_SETS = [
    ["fade", "dissolve", "smoothleft", "fade"],
    ["fadeblack", "circleopen", "fade", "slideleft"],
    ["smoothright", "fade", "radial", "dissolve"],
    ["wipeleft", "fade", "circlecrop", "smoothup"],
    ["fade", "hblur", "fadewhite", "dissolve"],
]
BASE_GRADES = ["warm", "soft", "golden", "vivid"]
CAMERA_SETS = [
    ["zoom_in", "pan_right", "tilt_up", "zoom_out", "diag_in"],
    ["pan_left", "zoom_in", "diag_out", "tilt_down", "pan_right"],
    ["zoom_out", "tilt_up", "pan_left", "zoom_in", "diag_in"],
]


def pick_style(seed_text: str, avoid: list[dict] | None = None) -> dict:
    """Per-video style signature, deterministic per project but different from the recent videos."""
    rnd = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:12], 16))
    avoid = avoid or []
    for _ in range(20):
        sig = {"transitions": rnd.randrange(len(TRANSITION_SETS)), "grade": rnd.choice(BASE_GRADES),
               "camera": rnd.randrange(len(CAMERA_SETS)), "subtitle_anim": rnd.choice(["pop", "karaoke"])}
        if sig not in avoid[-3:]:
            return sig
    return sig


# ---------------------------------------------------------------- layered scene render
@dataclass
class Sprite:
    path: Path
    x_frac: float  # centre x as fraction of frame width
    height_frac: float  # sprite height as fraction of frame height
    talk_windows: list[tuple[float, float]] = field(default_factory=list)  # seconds within the scene
    enter: str = "none"  # none|left|right
    phase: float = 0.0


def _windows_expr(windows: list[tuple[float, float]]) -> str:
    if not windows:
        return "0"
    return "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in windows[:12])


def layered_filter(*, W: int, H: int, fps: int, length: float, sprites: list[Sprite], pan_dir: int,
                   grade: str, light: str) -> str:
    """Filtergraph: input 0 = background (looped still), inputs 1..n = RGBA sprites."""
    zoom = 1.12
    bw, bh = int(W * zoom) // 2 * 2, int(H * zoom) // 2 * 2
    # background: slow horizontal parallax drift + very slow push-in (depth)
    x_expr = f"(iw-{W})*({0.5 + 0.45 * pan_dir}-{0.9 * pan_dir}*t/{length:.3f})" if pan_dir else f"(iw-{W})/2"
    g = [f"[0:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
         f"crop={W}:{H}:x='{x_expr}':y='(ih-{H})/2',setsar=1,fps={fps}[bg0]"]
    prev = "bg0"
    for i, sp in enumerate(sprites, start=1):
        sh = int(H * sp.height_frac) // 2 * 2
        talk = _windows_expr(sp.talk_windows)
        # idle: slow breathing scale; talking: quicker small bounce while their line plays
        # sprite is scaled once; idle "breathing" is a slow sway in the overlay position (cheap per frame)
        g.append(f"[{i}:v]format=rgba,scale=-2:{sh}[s{i}]")
        cx = f"{W * sp.x_frac:.1f}-w/2"
        # foreground moves opposite to the background drift -> parallax depth
        drift = f"-{pan_dir * W * 0.035:.1f}*t/{length:.3f}" if pan_dir else ""
        if sp.enter == "left":
            cx = f"if(lt(t,0.8),-w+({W * sp.x_frac:.1f}+w/2)*t/0.8,{cx})"
        elif sp.enter == "right":
            cx = f"if(lt(t,0.8),{W}-({W}-{W * sp.x_frac:.1f}+w/2)*t/0.8,{cx})"
        y = (f"{H * 0.97:.1f}-h-({talk})*abs({H * 0.012:.1f}*sin(2*PI*t*2.2+{sp.phase:.2f}))"
             f"-{H * 0.006:.1f}*sin(2*PI*t/2.8+{sp.phase:.2f})")
        sway = f"+{W * 0.003:.1f}*sin(2*PI*t/3.7+{sp.phase:.2f})"
        g.append(f"[{prev}][s{i}]overlay=x='{cx}{drift}{sway}':y='{y}':eval=frame:format=auto[bg{i}]")
        prev = f"bg{i}"
    g.append(f"[{prev}]{look_filter(grade, light)},format=yuv420p,trim=duration={length:.3f}[v]")
    return ";".join(g)


def camera_filter(motion: str, frames: int, W: int, H: int, fps: int) -> str:
    """Camera moves over a full illustration (animated-image storytelling)."""
    n = max(frames - 1, 1)
    z, cx, cy = "1.10", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "zoom_in":
        z = f"1+0.14*on/{n}"
    elif motion == "zoom_out":
        z = f"1.14-0.14*on/{n}"
    elif motion == "pan_left":
        cx = f"(iw-iw/zoom)*(1-on/{n})"
    elif motion == "pan_right":
        cx = f"(iw-iw/zoom)*on/{n}"
    elif motion == "tilt_up":
        cy = f"(ih-ih/zoom)*(1-on/{n})"
    elif motion == "tilt_down":
        cy = f"(ih-ih/zoom)*on/{n}"
    elif motion == "diag_in":
        z, cx, cy = f"1.04+0.12*on/{n}", f"(iw-iw/zoom)*0.3*on/{n}", f"(ih-ih/zoom)*0.25*on/{n}"
    elif motion == "diag_out":
        z, cx, cy = f"1.16-0.12*on/{n}", f"(iw-iw/zoom)*(0.6-0.3*on/{n})", f"(ih-ih/zoom)*(0.5-0.25*on/{n})"
    return (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
            f"zoompan=z='{z}':x='{cx}':y='{cy}':d={frames}:s={W}x{H}:fps={fps},setsar=1")


def compose_still(background: Path, sprites: list[Sprite], W: int, H: int, out: Path) -> Path:
    """Key-frame still of a layered scene (used for thumbnails, previews and duplicate checks)."""
    bg = Image.open(background).convert("RGB")
    scale = max(W / bg.width, H / bg.height)
    bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1), Image.LANCZOS)
    left, top = (bg.width - W) // 2, (bg.height - H) // 2
    canvas = bg.crop((left, top, left + W, top + H)).convert("RGBA")
    for sp in sprites:
        s = Image.open(sp.path).convert("RGBA")
        sh = int(H * sp.height_frac)
        s = s.resize((max(1, int(s.width * sh / s.height)), sh), Image.LANCZOS)
        canvas.alpha_composite(s, (int(W * sp.x_frac - s.width / 2), int(H * 0.97 - sh)))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out)
    return out


def positions(n: int) -> list[float]:
    return {1: [0.5], 2: [0.3, 0.7], 3: [0.2, 0.5, 0.8]}.get(n, [0.5])


def dhash(path: Path, size: int = 8) -> int:
    im = Image.open(path).convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(im.getdata())
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (px[r * (size + 1) + c] > px[r * (size + 1) + c + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ---------------------------------------------------------------- original generated sound
AMBIENCES = {
    # (lavfi source, filters) — synthesized here, so they are original with no third-party rights
    "rain": ("anoisesrc=color=pink:amplitude=0.6", "highpass=f=500,lowpass=f=9000"),
    "wind": ("anoisesrc=color=brown:amplitude=0.8", "bandpass=f=450:width_type=h:w=350,tremolo=f=0.18:d=0.7"),
    "water": ("anoisesrc=color=pink:amplitude=0.5", "bandpass=f=1300:width_type=h:w=1100,tremolo=f=0.6:d=0.35"),
    "fire": ("anoisesrc=color=brown:amplitude=0.7", "lowpass=f=900,tremolo=f=3.1:d=0.45"),
    "night": ("aevalsrc='0.18*sin(2*PI*4600*t)*gt(sin(2*PI*15*t),0.55)*gt(sin(2*PI*0.45*t),0.1)':s=48000", "highpass=f=3000"),
    "birds": ("aevalsrc='0.2*sin(2*PI*(2900+800*sin(2*PI*11*t))*t)*gt(sin(2*PI*0.6*t+1),0.88)"
              "+0.15*sin(2*PI*(3600+500*sin(2*PI*17*t))*t)*gt(sin(2*PI*0.37*t),0.92)':s=48000", "highpass=f=1500"),
    "magic": ("aevalsrc='(0.3*sin(2*PI*1318.5*t)*exp(-2.5*t)+0.22*sin(2*PI*1760*t)*exp(-2.0*t)"
              "+0.16*sin(2*PI*2637*t)*exp(-3.0*t))':s=48000", "aecho=0.8:0.6:120:0.35"),
}
AMBIENCE_WORDS = {
    "rain": ("rain", "drizzle", "monsoon", "மழை"), "wind": ("wind", "breeze", "storm", "காற்று"),
    "water": ("river", "water", "stream", "sea", "waves", "pond", "lake", "ஆறு"), "fire": ("fire", "campfire", "flame", "நெருப்பு"),
    "night": ("night", "crickets", "இரவு"), "birds": ("bird", "forest", "morning", "garden", "parrot", "பறவை"),
    "magic": ("magic", "sparkle", "spell", "glow", "chime", "மந்திர"),
}
LICENSE_ORIGINAL = "Original sound synthesized by YouTube AI Agent Pro (FFmpeg); no third-party rights"


def ambience_for(text: str) -> str | None:
    t = text.lower()
    return next((k for k, ws in AMBIENCE_WORDS.items() if any(w in t for w in ws)), None)


def synth_ambience(kind: str, seconds: float, out: Path) -> Path:
    src, flt = AMBIENCES[kind]
    secs = 2.5 if kind == "magic" else max(1.0, seconds)
    fade = f",afade=t=in:d=0.6,afade=t=out:st={max(0.1, secs - 0.8):.2f}:d=0.8" if kind != "magic" else ""
    out.parent.mkdir(parents=True, exist_ok=True)
    ff.run(["-f", "lavfi", "-i", src, "-af", f"{flt}{fade},aresample=48000", "-t", f"{secs:.2f}", "-ac", "1",
            "-c:a", "pcm_s16le", str(out)])
    return out


def synth_pad(seconds: float, out: Path, mood: str = "calm") -> Path:
    """Original gentle background pad (chord drones with slow movement) for videos without a licensed
    music track. Quiet by design; labelled as original generated audio."""
    chords = {"calm": (220.0, 277.18, 329.63), "mystery": (207.65, 246.94, 311.13), "bright": (261.63, 329.63, 392.0)}
    a, b, c = chords.get(mood, chords["calm"])
    expr = (f"0.16*sin(2*PI*{a}*t)*(0.6+0.4*sin(2*PI*0.07*t))+0.12*sin(2*PI*{b}*t)*(0.6+0.4*sin(2*PI*0.05*t+1))"
            f"+0.10*sin(2*PI*{c}*t)*(0.6+0.4*sin(2*PI*0.09*t+2))+0.05*sin(2*PI*{a / 2}*t)")
    ff.run(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000", "-af",
            f"lowpass=f=1800,aecho=0.8:0.7:400|700:0.25|0.18,afade=t=in:d=2,afade=t=out:st={max(0.1, seconds - 3):.2f}:d=3",
            "-t", f"{seconds:.2f}", "-ac", "2", "-c:a", "pcm_s16le", str(out)])
    return out
