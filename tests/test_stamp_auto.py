from pathlib import Path

import numpy as np

from app import recipes, render
from app.edits import DEFAULT_EDIT, normalize_edit
from app.stamp import contrast_color, load_mask

MASK = load_mask(Path(__file__).resolve().parents[1] / "app" / "assets" / "brm_highlife.png")
STAMP = {**DEFAULT_EDIT["stamp"], "color": "auto"}


def frame(corner_value: float, w=1200, h=800) -> np.ndarray:
    """Mid-grey frame whose bottom-right quarter (where the stamp sits) is `corner_value`."""
    rgb = np.full((h, w, 3), 0.5, np.float32)
    rgb[h // 2:, w // 2:] = corner_value
    return rgb


def test_contrast_color_picks_white_on_dark_and_black_on_light():
    assert contrast_color(frame(0.08), MASK, STAMP) == "#ffffff"
    assert contrast_color(frame(0.92), MASK, STAMP) == "#000000"
    # Only the stamp's corner counts: a dark top-left doesn't matter.
    rgb = frame(0.9)
    rgb[:400, :600] = 0.0
    assert contrast_color(rgb, MASK, {**STAMP, "position": "br"}) == "#000000"
    assert contrast_color(rgb, MASK, {**STAMP, "position": "tl"}) == "#ffffff"


def test_auto_stamp_renders_white_on_a_dark_photo():
    dark = render.from_array((np.full((800, 1200, 3), 20, np.uint8)))
    edit = normalize_edit({**DEFAULT_EDIT, "grade": {}, "stamp": STAMP})
    out = render.render(dark, edit, stamp_mask=MASK)
    corner = out[600:, 900:]
    assert corner.max() > 0.8                                # white letters on the dark corner
    light = render.from_array((np.full((800, 1200, 3), 235, np.uint8)))
    out = render.render(light, edit, stamp_mask=MASK)
    assert out[600:, 900:].min() < 0.2                      # black letters on the light corner


def test_auto_edit_turns_on_contrast_stamp():
    item = {"kind": "photo", "width": 1200, "height": 800}
    src = render.from_array(np.full((800, 1200, 3), 30, np.uint8))
    e = recipes.auto_edit(item, DEFAULT_EDIT, src, horizon=False, exposure=False)
    assert e["stamp"]["color"] == "auto"
    custom = {**DEFAULT_EDIT, "stamp": {**DEFAULT_EDIT["stamp"], "color": "#d4af37"}}
    assert recipes.auto_edit(item, custom, src, horizon=False, exposure=False)["stamp"]["color"] == "#d4af37"
