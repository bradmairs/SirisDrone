"""The BRM Highlife stamp (watermark): colouring, sizing, drop shadow and placement."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


@lru_cache(maxsize=4)
def _load_mask(path: str, mtime: float) -> Image.Image:
    """Alpha mask of the stamp artwork (any colour artwork works; alpha is what matters)."""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        mask = img.convert("RGBA").getchannel("A")
    else:
        # Opaque artwork: treat dark ink on a light background as the stamp.
        lum = np.asarray(img.convert("L"), np.float32)
        mask = Image.fromarray(np.clip((235 - lum) / 210 * 255, 0, 255).astype(np.uint8), "L")
    bbox = mask.point(lambda v: 255 if v > 8 else 0).getbbox()
    return mask.crop(bbox) if bbox else mask


def load_mask(path: Path) -> Image.Image:
    return _load_mask(str(path), path.stat().st_mtime)


_LOC_FONT_SIZE = 160   # mask resolution; the stamp is scaled to its on-image size later
_LOC_CAP_SCALE = 0.85  # all-caps reads bigger than mixed case: shrink to look the same size
_LOC_TRACKING = 0.12   # letter spacing, as a fraction of the font size
_LOC_BOLD = 3          # stroke width that makes the town bold
_LOC_COUNTRY = 0.7     # country is drawn at 70% so the town leads


def _tracked(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, font, fill: int,
             stroke: int = 0) -> float:
    """Draw letter-spaced text; returns the x after the last letter."""
    for ch in text:
        draw.text((x, y), ch, fill=fill, font=font, stroke_width=stroke, stroke_fill=fill, anchor="ls")
        x += font.getlength(ch) + _LOC_TRACKING * font.size
    return x


@lru_cache(maxsize=32)
def location_mask(text: str) -> Image.Image | None:
    """Alpha mask of the location: a thin accent bar, the town in bold spaced capitals and the
    country lighter, e.g. "| CAPE WOOLAMAI  AUSTRALIA". Pillow's built-in Aileron, so no font
    files are needed. The mask is one full line high whatever the letters, so every location
    is the same size on the image."""
    text = " ".join(text.split())
    if not text:
        return None
    town, _, country = (p.strip().upper() for p in text.partition(","))
    box = ImageFont.load_default(size=_LOC_FONT_SIZE)
    ascent, descent = box.getmetrics()
    font = ImageFont.load_default(size=round(_LOC_FONT_SIZE * _LOC_CAP_SCALE))
    cap = font.getbbox("H", anchor="ls")[1]            # negative: cap top above the baseline
    spacing = _LOC_TRACKING * font.size
    bar_w, gap, sep = round(font.size * 0.09), round(font.size * 0.4), round(font.size * 0.45)
    width_of = lambda t: sum(font.getlength(c) for c in t) + spacing * max(len(t) - 1, 0)
    width = 8 + bar_w + gap + width_of(town) + (sep + width_of(country) if country else 0) + 8 + _LOC_BOLD
    img = Image.new("L", (int(width), ascent + descent), 0)
    draw = ImageDraw.Draw(img)
    base = ascent                                       # baseline where it was before
    draw.rectangle((8, base + cap - 4, 8 + bar_w, base + 4), fill=255)
    x = _tracked(draw, 8 + bar_w + gap, base, town, font, 255, _LOC_BOLD)
    if country:
        _tracked(draw, x - spacing + sep, base, country, font, round(255 * _LOC_COUNTRY))
    return img


def location_settings(loc: dict, mask: Image.Image) -> dict:
    """The location's settings in render_stamp() terms (which sizes by width)."""
    return {"enabled": True, "position": loc["position"], "size": loc["size"] * mask.width / mask.height,
            "margin": loc["margin"], "opacity": loc["opacity"], "color": loc["color"],
            "shadow": loc.get("shadow", 0)}


AUTO = "auto"  # stamp colour chosen per image: black or white, whichever contrasts more


def _hex_rgb(color: str) -> tuple[int, int, int]:
    if color == AUTO:  # not resolved by the caller: fall back to the house black
        return 0, 0, 0
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def render_stamp(mask: Image.Image, out_w: int, out_h: int, s: dict) -> tuple[Image.Image, int, int]:
    """Return (RGBA stamp image, x, y) to composite onto an out_w × out_h frame."""
    short = min(out_w, out_h)
    logo_w = max(8, min(int(round(short * s["size"] / 100)), int(out_w * 0.95)))
    logo_h = max(2, int(round(mask.height * logo_w / mask.width)))
    alpha = mask.resize((logo_w, logo_h), Image.LANCZOS)
    opacity = s["opacity"] / 100.0

    shadow = s["shadow"] / 100.0
    blur = max(1.0, logo_h * 0.06)
    off = max(1, int(round(logo_h * 0.03)))
    pad = int(blur * 3) + off if shadow > 0 else 0
    cw, ch = logo_w + 2 * pad, logo_h + 2 * pad

    canvas = np.zeros((ch, cw, 4), np.float32)
    if shadow > 0:
        sh = Image.new("L", (cw, ch), 0)
        sh.paste(alpha, (pad + off, pad + off))
        sh = sh.filter(ImageFilter.GaussianBlur(blur))
        canvas[..., 3] = np.asarray(sh, np.float32) / 255 * min(1.0, shadow * 1.1) * 0.85
        # Shadow colour: the opposite of the stamp's brightness.
        r, g, b = _hex_rgb(s["color"])
        dark = (0.2126 * r + 0.7152 * g + 0.0722 * b) > 110
        canvas[..., :3] = 0.0 if dark else 1.0

    a = np.zeros((ch, cw), np.float32)
    a[pad:pad + logo_h, pad:pad + logo_w] = np.asarray(alpha, np.float32) / 255
    color = np.array(_hex_rgb(s["color"]), np.float32) / 255
    # "over" composite of the logo onto its shadow.
    sa = canvas[..., 3]
    out_a = a + sa * (1 - a)
    with np.errstate(invalid="ignore", divide="ignore"):
        rgb = (color * a[..., None] + canvas[..., :3] * (sa * (1 - a))[..., None]) / out_a[..., None]
    rgb = np.nan_to_num(rgb)
    out = np.dstack([rgb, out_a * opacity])
    img = Image.fromarray((np.clip(out, 0, 1) * 255).round().astype(np.uint8), "RGBA")

    margin = int(round(short * s["margin"] / 100))
    pos = s["position"]
    if pos[1] == "r":
        x = out_w - margin - logo_w - pad
    elif pos[1] == "l":
        x = margin - pad
    else:
        x = (out_w - logo_w) // 2 - pad
    y = (out_h - margin - logo_h - pad) if pos[0] == "b" else (margin - pad)
    return img, x, y


def contrast_color(rgb: np.ndarray, mask: Image.Image, s: dict) -> str:
    """Black or white, whichever has the higher contrast ratio against the pixels the
    stamp's letters will cover in this (graded, cropped) float RGB frame."""
    h, w = rgb.shape[:2]
    stamp, x, y = render_stamp(mask, w, h, {**s, "color": "#000000", "shadow": 0, "opacity": 100})
    a = np.asarray(stamp, np.float32)[..., 3] / 255
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + stamp.width, w), min(y + stamp.height, h)
    if x1 <= x0 or y1 <= y0:
        return "#000000"
    region = np.clip(rgb[y0:y1, x0:x1, :3], 0, 1)
    lin = np.where(region <= 0.04045, region / 12.92, ((region + 0.055) / 1.055) ** 2.4)
    lum = lin @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    wgt = a[y0 - y:y1 - y, x0 - x:x1 - x]
    L = float((lum * wgt).sum() / wgt.sum()) if wgt.sum() > 0 else float(lum.mean())
    return "#ffffff" if 1.05 / (L + 0.05) > (L + 0.05) / 0.05 else "#000000"


def resolve_color(rgb: np.ndarray, mask: Image.Image | None, s: dict) -> dict:
    """The stamp settings with an "auto" colour replaced by black or white for this frame."""
    if s.get("color") != AUTO or mask is None:
        return s
    return {**s, "color": contrast_color(rgb, mask, s)}


def composite(rgb: np.ndarray, stamp: Image.Image, x: int, y: int) -> np.ndarray:
    """Alpha-composite the stamp onto float RGB [H, W, 3] in place; returns rgb."""
    h, w = rgb.shape[:2]
    st = np.asarray(stamp, np.float32) / 255
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + stamp.width, w), min(y + stamp.height, h)
    if x1 <= x0 or y1 <= y0:
        return rgb
    part = st[y0 - y:y1 - y, x0 - x:x1 - x]
    a = part[..., 3:4]
    region = rgb[y0:y1, x0:x1]
    region *= 1 - a
    region += part[..., :3] * a
    return rgb
