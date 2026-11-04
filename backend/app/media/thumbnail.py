"""Thumbnail candidates (Pillow + libraqm for correct Tamil shaping) and deterministic selection.
YouTube: 1280x720 recommended (16:9), min width 640, JPG/PNG. Upload limit raised to 50MB in
Sept 2026; we stay well under 2MB for compatibility."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat, features

from app.config import TAMIL_FONT_BOLD

W, H = 1280, 720
MAX_BYTES = 2 * 1024 * 1024


@dataclass
class Candidate:
    path: Path
    text: str
    layout: str
    readability: float
    sharpness: float
    relevance: float

    @property
    def total(self) -> float:
        return round(0.5 * self.readability + 0.25 * self.sharpness + 0.25 * self.relevance, 3)


def _lum(rgb) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(a, b) -> float:
    la, lb = sorted([_lum(a), _lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _fit_font(draw, text, max_w, max_h, start=150):
    layout = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
    size = start
    while size > 30:
        f = ImageFont.truetype(str(TAMIL_FONT_BOLD), size, layout_engine=layout)
        box = draw.multiline_textbbox((0, 0), text, font=f, spacing=10, stroke_width=8)
        if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
            return f, box
        size -= 6
    return f, box


def _wrap(text: str, max_words: int = 3) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    mid = (len(words) + 1) // 2
    return " ".join(words[:mid]) + "\n" + " ".join(words[mid:])


def make_candidate(bg: Path, text: str, layout: str, out: Path, title_words: set[str]) -> Candidate:
    img = Image.open(bg).convert("RGB")
    s = max(W / img.width, H / img.height)
    img = img.resize((int(img.width * s + 0.5), int(img.height * s + 0.5)), Image.LANCZOS)
    l, t = (img.width - W) // 2, (img.height - H) // 2
    img = img.crop((l, t, l + W, t + H))
    img = ImageEnhance.Color(img).enhance(1.25)
    img = ImageEnhance.Contrast(img).enhance(1.1)
    text = _wrap(text)
    # text band region by layout
    band = {"bottom": (0, int(H * 0.58), W, H), "left": (0, 0, int(W * 0.55), H), "top": (0, 0, W, int(H * 0.42))}[layout]
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    bx0, by0, bx1, by1 = band
    for i in range(0, 100):  # gradient scrim for legibility
        a = int(200 * (i / 100))
        if layout == "bottom":
            y = by0 + (by1 - by0) * i / 100
            od.rectangle([0, y, W, y + (by1 - by0) / 100 + 1], fill=(0, 0, 0, a))
        elif layout == "top":
            y = by1 - (by1 - by0) * i / 100
            od.rectangle([0, y - (by1 - by0) / 100 - 1, W, y], fill=(0, 0, 0, a))
        else:
            x = bx1 - (bx1 - bx0) * i / 100
            od.rectangle([x - (bx1 - bx0) / 100 - 1, 0, x, H], fill=(0, 0, 0, a))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    img_no_text = img.copy()
    d = ImageDraw.Draw(img)
    pad = 48
    font, box = _fit_font(d, text, (bx1 - bx0) - 2 * pad, (by1 - by0) - 2 * pad)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = bx0 + ((bx1 - bx0) - tw) / 2 - box[0] if layout != "left" else bx0 + pad - box[0]
    y = by0 + ((by1 - by0) - th) / 2 - box[1]
    fill = (255, 214, 10)
    d.multiline_text((x, y), text, font=font, fill=fill, stroke_width=8, stroke_fill=(10, 10, 10), spacing=10,
                     align="center" if layout != "left" else "left")
    # readability: contrast of text colour against the region behind the text + size factor
    # background luminance behind the text box (after scrim), measured on a copy without the text
    box_bg = ImageStat.Stat(img_no_text.crop((int(max(0, x)), int(max(0, y)), int(min(W, x + tw)), int(min(H, y + th))))).mean
    cr = contrast_ratio(fill, tuple(int(c) for c in box_bg))
    size_factor = min(1.0, font.size / 110)
    readability = round(min(1.0, cr / 15) * 0.5 + size_factor * 0.5, 3)
    edges = ImageStat.Stat(img.convert("L").filter(ImageFilter.FIND_EDGES)).var[0]
    sharpness = round(min(1.0, edges / 1500), 3)
    words = set(text.replace("\n", " ").split())
    relevance = round(len(words & title_words) / max(1, len(words)), 3) if title_words else 0.5
    q = 92
    while True:
        img.save(out, "JPEG", quality=q, optimize=True, progressive=True)
        if out.stat().st_size <= MAX_BYTES or q < 50:
            break
        q -= 8
    return Candidate(out, text, layout, readability, sharpness, relevance)


def generate_candidates(backgrounds: list[Path], texts: list[str], out_dir: Path, title: str) -> tuple[Candidate, list[Candidate]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    layouts = ["bottom", "left", "top"]
    title_words = set(title.split())
    cands = []
    for i, text in enumerate(texts[:3]):
        bg = backgrounds[i % len(backgrounds)]
        cands.append(make_candidate(bg, text, layouts[i % 3], out_dir / f"thumb_{i}.jpg", title_words))
    best = max(cands, key=lambda c: c.total)
    return best, cands


def validate_thumbnail(path: Path) -> list[str]:
    problems = []
    im = Image.open(path)
    if im.width < 640:
        problems.append("width < 640px")
    if abs(im.width / im.height - 16 / 9) > 0.01:
        problems.append("aspect is not 16:9")
    if im.format not in ("JPEG", "PNG"):
        problems.append(f"format {im.format} not JPG/PNG")
    if path.stat().st_size > MAX_BYTES:
        problems.append("larger than 2MB")
    return problems
