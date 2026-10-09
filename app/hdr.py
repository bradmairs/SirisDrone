"""Merge exposure brackets (e.g. Mini 3 Pro AEB, 3 or 5 shots) into one image.

Exposure fusion (Mertens et al.): each pixel is a weighted blend of the brackets,
favouring well-exposed, saturated, detailed pixels. Weights are computed at low
resolution and smoothed, which avoids seams/halos and keeps memory low for 48 MP shots.
The frames are aligned first (phase correlation) to remove small drone drift.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from . import render

WORK_EDGE = 1024  # resolution the weights are computed at


def _gray_small(rgb8: np.ndarray, edge: int) -> np.ndarray:
    im = Image.fromarray(rgb8).convert("L")
    k = edge / max(im.size)
    im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.BILINEAR)
    return np.asarray(im, np.float32) / 255


def _shift(ref: np.ndarray, img: np.ndarray) -> tuple[int, int]:
    """(dy, dx) that best aligns img to ref (both same-size grey arrays), by phase correlation."""
    a = ref - ref.mean()
    b = img - img.mean()
    # Equalise brightness differences between exposures before correlating.
    a /= a.std() + 1e-6
    b /= b.std() + 1e-6
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    F = np.fft.fft2(a * win) * np.conj(np.fft.fft2(b * win))
    r = np.fft.ifft2(F / (np.abs(F) + 1e-9)).real
    dy, dx = np.unravel_index(int(r.argmax()), r.shape)
    if dy > a.shape[0] // 2:
        dy -= a.shape[0]
    if dx > a.shape[1] // 2:
        dx -= a.shape[1]
    return int(dy), int(dx)


def _to_rgb8(src) -> np.ndarray:
    return render.to_uint8(render.to_float(src)) if not isinstance(src, Image.Image) else np.asarray(src.convert("RGB"))


def _box(a: np.ndarray, r: int) -> np.ndarray:
    if r < 1:
        return a
    out = a
    for axis in (0, 1):
        p = np.pad(out, [(r, r) if i == axis else (0, 0) for i in range(2)], mode="edge")
        c = np.cumsum(p, axis=axis, dtype=np.float64)
        c = np.concatenate([np.zeros_like(c.take([0], axis=axis)), c], axis=axis)
        n = out.shape[axis]
        size = 2 * r + 1
        out = ((c.take(np.arange(size, size + n), axis=axis) - c.take(np.arange(0, n), axis=axis)) / size)
    return out.astype(np.float32)


def merge(sources: list, progress=None) -> np.ndarray:
    """Fuse bracketed shots (render sources, any order) into float RGB [H, W, 3]."""
    if len(sources) < 2:
        raise ValueError("Select at least two bracketed photos")
    frames = [_to_rgb8(s) for s in sources]
    h, w = frames[0].shape[:2]
    if any(f.shape[:2] != (h, w) for f in frames):
        raise ValueError("Bracketed photos must all be the same size")
    # Order by brightness; align everything to the middle exposure.
    frames.sort(key=lambda f: float(_gray_small(f, 256).mean()))
    ref_i = len(frames) // 2
    scale = max(h, w) / WORK_EDGE
    small = [_gray_small(f, WORK_EDGE) for f in frames]
    shifts = [(0, 0) if i == ref_i else tuple(round(v * scale) for v in _shift(small[ref_i], small[i]))
              for i in range(len(frames))]
    # Crop every frame to the region all of them cover.
    top = max(max(dy, 0) for dy, _ in shifts)
    left = max(max(dx, 0) for _, dx in shifts)
    bottom = h + min(min(dy, 0) for dy, _ in shifts)
    right = w + min(min(dx, 0) for _, dx in shifts)
    if bottom - top < h * 0.8 or right - left < w * 0.8:
        raise ValueError("The photos are too far apart to merge – were they shot as one bracket?")
    frames = [f[top - dy:bottom - dy, left - dx:right - dx] for f, (dy, dx) in zip(frames, shifts)]
    H, W = frames[0].shape[:2]
    if progress:
        progress(0.3)

    # Fusion weights at working resolution.
    ws = []
    for f in frames:
        s = np.asarray(Image.fromarray(f).resize((max(1, round(W / scale)), max(1, round(H / scale))),
                                                 Image.BILINEAR), np.float32) / 255
        g = s @ np.array([0.299, 0.587, 0.114], np.float32)
        lap = np.abs(4 * g - np.roll(g, 1, 0) - np.roll(g, -1, 0) - np.roll(g, 1, 1) - np.roll(g, -1, 1))
        contrast = _box(lap, 2) + 1e-3
        saturation = s.std(axis=2) + 1e-3
        exposed = np.prod(np.exp(-((s - 0.5) ** 2) / (2 * 0.2 ** 2)), axis=2) + 1e-6
        ws.append(contrast * saturation * exposed)
    ws = np.stack(ws)
    ws = np.stack([_box(wi, max(2, round(WORK_EDGE / 120))) for wi in ws])  # smooth: no seams
    ws /= ws.sum(axis=0, keepdims=True)

    # Blend at full resolution, in strips to keep memory bounded.
    full_w = [np.asarray(Image.fromarray(wi, "F").resize((W, H), Image.BILINEAR), np.float32) for wi in ws]
    out = np.zeros((H, W, 3), np.float32)
    step = 512
    for y in range(0, H, step):
        acc = out[y:y + step]
        for f, wf in zip(frames, full_w):
            acc += f[y:y + step].astype(np.float32) * (wf[y:y + step, :, None] / 255)
        if progress:
            progress(0.3 + 0.65 * min(1, (y + step) / H))
    return np.clip(out, 0, 1)
