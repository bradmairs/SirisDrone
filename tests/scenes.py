"""Synthetic drone-like test scenes (no real photos needed in CI)."""
import numpy as np
from PIL import Image, ImageFilter


def scene(seed: int, w: int = 1600, h: int = 1200, smooth: bool = False) -> Image.Image:
    """Sky over textured land/sea with a horizon; `smooth` gives a calm, low-texture sea shot."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    tex = np.zeros((h, w), np.float32)
    for s, amp in ((8, 1.0), (24, 0.6), (80, 0.4)):
        n = rng.random((h // s + 2, w // s + 2)).astype(np.float32)
        tex += amp * np.asarray(Image.fromarray(n).resize((w + s, h + s), Image.BICUBIC))[:h, :w]
    tex /= tex.max()
    hz = h * (0.3 + 0.2 * rng.random())
    sky = np.stack([0.45 + 0 * x, 0.65 + 0 * x, 0.92 - 0.1 * y / h], -1)
    k = 0.04 if smooth else 0.35
    ground = np.stack([0.1 + k * tex, 0.3 + k * tex, 0.45 + 0.5 * k * tex], -1)
    img = np.where((y > hz)[..., None], ground, sky)
    if not smooth:  # a few hard-edged rocks / buildings
        for _ in range(12):
            cx, cy, r = rng.integers(0, w), rng.integers(int(hz), h), rng.integers(15, 60)
            img[max(cy - r, 0):cy + r, max(cx - r, 0):cx + r] = rng.random(3) * 0.8 + 0.1
    return Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))


def shaken(img: Image.Image, px: int = 6) -> Image.Image:
    """Motion blur from a gust: average of shifted copies."""
    a = np.asarray(img, np.float32)
    out = sum(np.roll(a, i, axis=1) for i in range(px)) / px
    return Image.fromarray(out.astype(np.uint8))


def jitter(img: Image.Image, seed: int) -> Image.Image:
    """Next frame of a burst: tiny drift + sensor noise."""
    rng = np.random.default_rng(seed)
    a = np.roll(np.asarray(img, np.float32), (int(rng.integers(-3, 4)), int(rng.integers(-3, 4))), (0, 1))
    a += rng.normal(0, 2.0, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
