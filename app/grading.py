"""Colour grading.

Every colour adjustment is a per-pixel function of RGB, so the whole grade is
baked into a single 3D LUT. Photos, video (via FFmpeg's lut3d filter) and the
live preview all use that same LUT, which keeps them visually identical.

Values are display-referred RGB in [0, 1] (Rec.709 / sRGB).
"""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import numpy as np

_POOL = ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2))
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

DEFAULT_GRADE: dict = {
    "exposure": 0.0,     # EV, -3..3
    "contrast": 0,       # -100..100
    "highlights": 0,     # -100..100
    "shadows": 0,        # -100..100
    "whites": 0,         # -100..100
    "blacks": 0,         # -100..100
    "temperature": 0,    # -100..100  (+ warmer)
    "tint": 0,           # -100..100  (+ magenta)
    "vibrance": 0,       # -100..100
    "saturation": 0,     # -100..100
    "dehaze": 0,         # -100..100
    "fade": 0,           # 0..100
    "shadow_hue": 210,   # degrees
    "shadow_sat": 0,     # 0..100
    "highlight_hue": 35, # degrees
    "highlight_sat": 0,  # 0..100
    "blue_sat": 0,       # -100..100  saturation of sky/water blues only
    "blue_lum": 0,       # -100..100  brightness of blues (negative = deeper)
    "lut": None,         # id of an uploaded .cube LUT
    "lut_strength": 100, # 0..100
}

# The BRM Highlife house look: rich, clear sky & ocean blues with natural greens and sand.
# New uploads start with this grade (see edits.DEFAULT_EDIT).
HOUSE_GRADE: dict = {"exposure": 0.05, "temperature": -15, "tint": 6, "contrast": 28,
                     "highlights": -20, "whites": 5, "blacks": -6, "dehaze": 15,
                     "vibrance": 40, "saturation": 15, "blue_sat": 40, "blue_lum": 12}

PRESETS: dict[str, dict] = {
    "BRM Blue": HOUSE_GRADE,
    "Natural": {},
    "Vivid Aerial": {"contrast": 18, "highlights": -35, "shadows": 25, "vibrance": 35,
                     "saturation": 8, "dehaze": 25, "whites": 8, "blacks": -8},
    "Clear Skies": {"highlights": -55, "shadows": 15, "dehaze": 35, "vibrance": 25,
                    "temperature": -6, "contrast": 10},
    "Golden Hour": {"temperature": 32, "tint": 6, "contrast": 12, "highlights": -25,
                    "shadows": 18, "vibrance": 20, "highlight_hue": 38, "highlight_sat": 25,
                    "shadow_hue": 20, "shadow_sat": 8},
    "Teal & Orange": {"contrast": 22, "highlights": -20, "shadows": 10, "saturation": 5,
                      "vibrance": 15, "shadow_hue": 190, "shadow_sat": 35,
                      "highlight_hue": 30, "highlight_sat": 30},
    "Moody": {"exposure": -0.3, "contrast": 25, "highlights": -40, "shadows": -10,
              "saturation": -25, "fade": 20, "temperature": -10, "shadow_hue": 205,
              "shadow_sat": 20},
    "Cinematic Fade": {"contrast": 12, "fade": 35, "saturation": -12, "highlights": -20,
                       "shadow_hue": 200, "shadow_sat": 18, "highlight_hue": 40,
                       "highlight_sat": 15},
    "Tropical": {"temperature": 8, "vibrance": 45, "saturation": 10, "dehaze": 20,
                 "contrast": 14, "highlights": -30, "shadows": 15, "tint": -4},
    "Black & White": {"saturation": -100, "contrast": 30, "highlights": -20, "shadows": 15,
                      "blacks": -10},
    "D-Log M → Rec.709 (quick)": {"contrast": 55, "saturation": 35, "vibrance": 15,
                                   "blacks": -35, "whites": 25, "highlights": -10,
                                   "exposure": 0.15},
}


def normalize_grade(grade: dict | None) -> dict:
    """Fill in a grade; missing keys are neutral (0), not the house look."""
    g = dict(DEFAULT_GRADE)
    if grade:
        for k, v in grade.items():
            if k in g:
                g[k] = v
    for k, v in g.items():
        if k == "lut":
            g[k] = str(v) if v else None
        else:
            g[k] = float(v or 0)
    return g


def is_neutral(grade: dict) -> bool:
    g = normalize_grade(grade)
    for k, v in g.items():
        if k in ("shadow_hue", "highlight_hue", "lut_strength"):
            continue
        if k == "lut":
            if v and g["lut_strength"] > 0:
                return False
        elif v != 0:
            return False
    return True


# ---------------------------------------------------------------------------
# .cube LUT files
# ---------------------------------------------------------------------------

def read_cube(path: str | Path) -> np.ndarray:
    """Read a 3D .cube LUT. Returns float32 array [b, g, r, 3] with values for domain 0..1."""
    size = None
    dmin = np.zeros(3, np.float32)
    dmax = np.ones(3, np.float32)
    rows: list[list[float]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            head = line.split()[0].upper()
            if head == "TITLE":
                continue
            if head == "LUT_3D_SIZE":
                size = int(line.split()[1])
            elif head == "LUT_1D_SIZE":
                raise ValueError("1D LUTs are not supported, please use a 3D .cube LUT")
            elif head == "DOMAIN_MIN":
                dmin = np.array([float(x) for x in line.split()[1:4]], np.float32)
            elif head == "DOMAIN_MAX":
                dmax = np.array([float(x) for x in line.split()[1:4]], np.float32)
            elif head[0].isalpha():
                continue  # unknown keyword (e.g. LUT_3D_INPUT_RANGE)
            else:
                parts = line.split()
                if len(parts) >= 3:
                    rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
    if not size:
        raise ValueError("Not a 3D .cube file (missing LUT_3D_SIZE)")
    if len(rows) != size ** 3:
        raise ValueError(f"Expected {size ** 3} LUT entries, found {len(rows)}")
    lut = np.asarray(rows, np.float32).reshape(size, size, size, 3)
    if not (np.allclose(dmin, 0) and np.allclose(dmax, 1)):
        # Rescale a non-unit domain onto 0..1 by resampling.
        grid = identity_grid(size)
        lut = apply_lut(dmin + grid * (dmax - dmin), lut, dmin, dmax)
    return lut


def write_cube(path: str | Path, lut: np.ndarray, title: str = "SirisDrone grade") -> None:
    n = lut.shape[0]
    flat = np.clip(lut.reshape(-1, 3), 0.0, 1.0)
    with open(path, "w", encoding="ascii") as fh:
        fh.write(f'TITLE "{title}"\nLUT_3D_SIZE {n}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        np.savetxt(fh, flat, fmt="%.6f")


def identity_grid(n: int) -> np.ndarray:
    """[b, g, r, 3] grid of RGB values, r varying fastest (the .cube ordering)."""
    v = np.linspace(0.0, 1.0, n, dtype=np.float32)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")
    return np.stack([r, g, b], axis=-1)


def _lut_chunk(src, out, flat_lut, n, dmin, scale):
    p = (src - dmin) * scale
    np.clip(p, 0, n - 1, out=p)
    i0 = np.minimum(p.astype(np.int32), n - 2)
    f = p - i0
    fr, fg, fb = f[:, 0:1], f[:, 1:2], f[:, 2:3]
    base = (i0[:, 2] * n + i0[:, 1]) * n + i0[:, 0]
    sg, sb = n, n * n

    def lerp_r(off):
        a = flat_lut[base + off]
        return a + (flat_lut[base + off + 1] - a) * fr

    c0 = lerp_r(0)
    c0 += (lerp_r(sg) - c0) * fg
    c1 = lerp_r(sb)
    c1 += (lerp_r(sb + sg) - c1) * fg
    out[:] = c0 + (c1 - c0) * fb


def apply_lut(img: np.ndarray, lut: np.ndarray, dmin=0.0, dmax=1.0,
              chunk: int = 1 << 16, out: np.ndarray | None = None) -> np.ndarray:
    """Trilinear 3D LUT lookup. `img` is float [..., 3]; returns float32 of the same shape.

    Pass out=img to work in place (saves a full-size copy on big photos).
    """
    n = lut.shape[0]
    flat_lut = np.ascontiguousarray(lut.reshape(-1, 3), dtype=np.float32)
    src = np.asarray(img, dtype=np.float32)
    shape = src.shape
    src = src.reshape(-1, 3)
    out = np.empty_like(src) if out is None else out.reshape(-1, 3)
    dmin = np.asarray(dmin, np.float32)
    scale = (n - 1) / (np.asarray(dmax, np.float32) - dmin)
    starts = range(0, src.shape[0], chunk)
    work = lambda s: _lut_chunk(src[s:s + chunk], out[s:s + chunk], flat_lut, n, dmin, scale)
    if len(starts) > 2:
        list(_POOL.map(work, starts))  # numpy releases the GIL for most of this
    else:
        for s in starts:
            work(s)
    return out.reshape(shape)


# ---------------------------------------------------------------------------
# The grade itself
# ---------------------------------------------------------------------------

def _srgb_to_linear(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(x):
    x = np.maximum(x, 0.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def _hue_rgb(hue_deg: float) -> np.ndarray:
    """Fully saturated colour for a hue, zero-mean so it shifts colour but not brightness."""
    h = (hue_deg % 360) / 60.0
    x = 1 - abs(h % 2 - 1)
    rgb = [(1, x, 0), (x, 1, 0), (0, 1, x), (0, x, 1), (x, 0, 1), (1, 0, x)][int(h) % 6]
    rgb = np.array(rgb, np.float32)
    return rgb - float(rgb @ LUMA)


def _blue_weight(x: np.ndarray) -> np.ndarray:
    """0..1: how much a pixel belongs to the cyan→blue range (hue ~165°–255°, centred on 210°)."""
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    hue = np.degrees(np.arctan2(np.sqrt(3) * (g - b), 2 * r - g - b)) % 360
    t = np.clip(1 - np.abs(hue - 210) / 45, 0, 1)
    chroma = x.max(axis=-1) - x.min(axis=-1)
    return t * t * (3 - 2 * t) * np.clip(chroma * 5, 0, 1)


def grade_pixels(rgb: np.ndarray, g: dict, user_lut: np.ndarray | None = None) -> np.ndarray:
    """Apply the grade directly to float RGB [..., 3] in 0..1."""
    x = np.clip(np.asarray(rgb, np.float32), 0.0, 1.0)

    # 1. Technical / creative LUT first (e.g. D-Log M -> Rec.709).
    if user_lut is not None and g["lut_strength"] > 0:
        s = g["lut_strength"] / 100.0
        x = x + (apply_lut(x, user_lut) - x) * s
        x = np.clip(x, 0.0, 1.0)

    # 2. White balance + exposure in linear light.
    lin = _srgb_to_linear(x)
    t, tn = g["temperature"] / 100.0, g["tint"] / 100.0
    if t or tn or g["exposure"]:
        gains = np.array([2 ** (0.32 * t), 2 ** (-0.28 * tn), 2 ** (-0.32 * t)], np.float32)
        gains /= float(gains @ LUMA)  # keep brightness constant
        lin = lin * gains * (2.0 ** g["exposure"])

    # 3. Dehaze: remove (or add) a bluish atmospheric veil.
    d = g["dehaze"] / 100.0
    if d:
        air = np.array([0.80, 0.86, 0.95], np.float32)
        k = 0.22 * d
        if k > 0:
            lin = (lin - k * air * 0.35) / (1 - k * 0.35)
        else:
            lin = lin * (1 + k * 0.35) - k * air * 0.35
        lin = np.maximum(lin, 0.0)

    x = np.clip(_linear_to_srgb(lin), 0.0, 1.0)

    # 4. Tone: blacks/whites, shadows/highlights, contrast, fade.
    b1 = 0.10 * g["blacks"] / 100.0
    w1 = 1.0 + 0.12 * g["whites"] / 100.0
    if b1 or w1 != 1.0:
        x = np.clip(b1 + x * (w1 - b1), 0.0, 1.0)
    s = g["shadows"] / 100.0
    if s:
        x = x + s * (1.69 if s > 0 else 0.9) * x * (1 - x) ** 2
    h = g["highlights"] / 100.0
    if h:
        x = x + h * (0.9 if h > 0 else 1.69) * x * x * (1 - x)
    c = g["contrast"] / 100.0
    if c:
        x = np.clip(x, 0.0, 1.0)
        x = x + c * (x * x * (3 - 2 * x) - x)
    f = g["fade"] / 100.0
    if f:
        x = x + 0.14 * f * (1 - x) ** 2 - 0.04 * f * x * x
    x = np.clip(x, 0.0, 1.0)

    # 5. Saturation & vibrance (around Rec.709 luma).
    y = x @ LUMA
    chroma = x - y[..., None]
    sat = g["saturation"] / 100.0
    vib = g["vibrance"] / 100.0
    if sat or vib:
        amount = 1.0 + sat
        if vib:
            current = x.max(axis=-1) - x.min(axis=-1)
            amount = amount + vib * (1 - np.clip(current * 1.6, 0, 1)) ** 2 * (1.2 if vib > 0 else 1.0)
            amount = np.maximum(amount, 0)[..., None]
        x = y[..., None] + chroma * amount

    # 6. Blues only (sky & water): leaves greens, sand, rock and skin alone.
    bs, bl = g["blue_sat"] / 100.0, g["blue_lum"] / 100.0
    if bs or bl:
        w = _blue_weight(x)[..., None]
        if bs:
            y = x @ LUMA
            x = y[..., None] + (x - y[..., None]) * (1 + bs * w)
        if bl:
            x = x * (1 + 0.35 * bl * w)
        y = x @ LUMA

    # 7. Split toning.
    if g["shadow_sat"] or g["highlight_sat"]:
        yy = np.clip(y, 0, 1)[..., None]
        if g["shadow_sat"]:
            x = x + _hue_rgb(g["shadow_hue"]) * (0.18 * g["shadow_sat"] / 100.0) * (1 - yy) ** 2
        if g["highlight_sat"]:
            x = x + _hue_rgb(g["highlight_hue"]) * (0.18 * g["highlight_sat"] / 100.0) * yy ** 2

    return np.clip(x, 0.0, 1.0).astype(np.float32)


def grade_key(grade: dict) -> str:
    return hashlib.sha1(json.dumps(normalize_grade(grade), sort_keys=True).encode()).hexdigest()[:16]


_lut_loader = None  # set by the app: callable(lut_id) -> Path | None


def set_lut_loader(fn) -> None:
    global _lut_loader
    _lut_loader = fn
    _build_lut.cache_clear()


@lru_cache(maxsize=64)
def _cached_user_lut(path: str, mtime: float) -> np.ndarray:
    return read_cube(path)


def load_user_lut(lut_id: str | None) -> np.ndarray | None:
    if not lut_id or _lut_loader is None:
        return None
    p = _lut_loader(lut_id)
    if p is None or not Path(p).exists():
        return None
    return _cached_user_lut(str(p), Path(p).stat().st_mtime)


@lru_cache(maxsize=32)
def _build_lut(key_json: str, n: int) -> np.ndarray:
    g = json.loads(key_json)
    return grade_pixels(identity_grid(n), g, load_user_lut(g["lut"]))


def build_lut(grade: dict, n: int = 33) -> np.ndarray:
    """Bake the grade into an n^3 LUT, [b, g, r, 3]."""
    return _build_lut(json.dumps(normalize_grade(grade), sort_keys=True), n)


def grade_image(rgb: np.ndarray, grade: dict, n: int = 33, in_place: bool = False) -> np.ndarray:
    if is_neutral(grade):
        return np.asarray(rgb, np.float32)
    return apply_lut(rgb, build_lut(grade, n), out=rgb if in_place else None)
