"""Convert the BRM Highlife logo (black script on white) into a transparent stamp.

Usage:  python tools/make_stamp.py [source.jpg] [dest.png]

The text is turned into an alpha mask, trimmed, upscaled 2x so it stays crisp on
high-resolution exports, and filled white. The app re-colours it at render time.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "assets" / "brm_highlife_source.jpg"
DST = ROOT / "app" / "assets" / "brm_highlife.png"


def make_stamp(src: Path, dst: Path, upscale: int = 2) -> Image.Image:
    lum = np.asarray(Image.open(src).convert("L"), dtype=np.float32)
    # White paper -> transparent, ink -> opaque, with a soft ramp for anti-aliasing.
    alpha = np.clip((235.0 - lum) / (235.0 - 25.0), 0.0, 1.0)
    ys, xs = np.where(alpha > 0.02)
    pad = 6
    y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad + 1, alpha.shape[0])
    x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad + 1, alpha.shape[1])
    alpha = alpha[y0:y1, x0:x1]

    mask = Image.fromarray((alpha * 255).round().astype(np.uint8), "L")
    mask = mask.resize((mask.width * upscale, mask.height * upscale), Image.LANCZOS)
    # Re-tighten the edge ramp that upscaling softened.
    a = np.asarray(mask, dtype=np.float32) / 255.0
    a = np.clip((a - 0.12) / 0.76, 0.0, 1.0)
    a = a * a * (3 - 2 * a)

    out = np.zeros(a.shape + (4,), dtype=np.uint8)
    out[..., :3] = 255
    out[..., 3] = (a * 255).round().astype(np.uint8)
    img = Image.fromarray(out, "RGBA")
    img.save(dst, optimize=True)
    return img


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else DST
    img = make_stamp(src, dst)
    print(f"wrote {dst} ({img.width}x{img.height})")
