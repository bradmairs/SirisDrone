"""Automatic adjustments: level the horizon and set exposure."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

from . import grading, render
from .edits import normalize_edit

MAX_TILT = 12.0  # degrees; drone horizons are rarely further off than this


def _gray(img: Image.Image, edge: int = 800) -> np.ndarray:
    img = img.convert("L")
    img.thumbnail((edge, edge), Image.BILINEAR)
    # Soften fine texture (surf, rocks, trees) so long, smooth edges like the horizon win.
    img = img.filter(ImageFilter.GaussianBlur(2))
    return np.asarray(img, np.float32) / 255


def detect_tilt(img: Image.Image) -> tuple[float, float] | None:
    """Angle (degrees, +ve = right side lower) of the dominant near-horizontal line, and a
    0..1 confidence; None when there is no clear horizon (e.g. straight-down shots)."""
    g = _gray(img)
    h, w = g.shape
    gx = np.zeros_like(g)
    gy = np.zeros_like(g)
    gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    gy[1:-1, :] = g[2:, :] - g[:-2, :]
    mag = np.hypot(gx, gy)
    horiz = np.abs(gy) > 2.0 * np.abs(gx)            # edge runs roughly left-right
    border = np.zeros_like(horiz)
    border[3:-3, 3:-3] = True
    cand = mag * horiz * border
    thresh = np.quantile(cand[cand > 0], 0.85) if np.any(cand > 0) else 0
    ys, xs = np.nonzero(cand > max(thresh, 0.01))
    if len(xs) < 40:
        return None
    if len(xs) > 8000:
        pick = np.random.default_rng(0).choice(len(xs), 8000, replace=False)
        ys, xs = ys[pick], xs[pick]
    wts = cand[ys, xs]
    xs_c = xs - w / 2
    angles = np.arange(-MAX_TILT, MAX_TILT + 0.001, 0.1)
    best = (-1.0, 0.0, None)
    scores = []
    for a in angles:
        b = ys - xs_c * np.tan(np.radians(a))           # intercept of a line at this angle
        hist = np.bincount(np.clip(b + h, 0, 3 * h).astype(int), weights=wts, minlength=3 * h + 1)
        peak = np.convolve(hist, np.ones(3), "same")
        k = int(peak.argmax())
        scores.append(peak[k])
        if peak[k] > best[0]:
            best = (float(peak[k]), float(a), k)
    score, angle, k = best
    # The winning line must span a good part of the frame and stand out from other angles.
    on_line = np.abs((ys - xs_c * np.tan(np.radians(angle))) + h - k) <= 1.5
    span = (xs[on_line].max() - xs[on_line].min()) / w if on_line.any() else 0
    contrast = score / (np.median(scores) + 1e-6)
    confidence = float(np.clip((span - 0.5) / 0.4, 0, 1) * np.clip((contrast - 1.2) / 0.8, 0, 1))
    if confidence < 0.2:
        return None
    return angle, confidence


def auto_straighten(src, edit: dict) -> float | None:
    """Straighten value that levels the horizon, keeping the item's 90° rotation and flips."""
    e = normalize_edit({**edit, "straighten": 0, "crop": {"x": 0, "y": 0, "w": 1, "h": 1}})
    rgb = render.render(src, {**e, "grade": {}}, long_edge=900, mode="frame", fast=True)
    found = detect_tilt(Image.fromarray(render.to_uint8(rgb)))
    if not found:
        return None
    return round(-found[0], 1)  # rotate against the tilt


def auto_exposure(src, edit: dict) -> float:
    """Exposure (EV) that puts the mid-tones around 45% without clipping highlights,
    measured through the item's own grade."""
    e = normalize_edit(edit)
    small = render.render(src, {**e, "grade": {**e["grade"], "exposure": 0}, "stamp": {"enabled": False},
                                "vignette": 0, "grad": {"strength": 0}},
                          long_edge=320, fast=True)
    lut_free = small.reshape(-1, 3)

    user_lut = grading.load_user_lut(e["grade"]["lut"])

    def stats(ev):
        out = grading.grade_pixels(lut_free, {**e["grade"], "exposure": ev}, user_lut)
        # mid-tones by brightness; highlights by the brightest channel (blue skies clip first)
        return float(np.median(out @ grading.LUMA)), float(np.quantile(out.max(axis=1), 0.995))

    def solve(fn, target):
        lo, hi = -2.0, 2.0
        for _ in range(18):
            mid = (lo + hi) / 2
            if fn(mid) < target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    ev_mid = solve(lambda ev: stats(ev)[0], 0.42)
    ev_hi = solve(lambda ev: stats(ev)[1], 0.98)
    ev = min(ev_mid, max(ev_hi, 0.0)) if ev_mid > 0 else ev_mid  # never brighten into clipping
    return round(float(np.clip(ev, -1.5, 1.5)) / 0.05) * 0.05
