"""The edit document attached to each media item, and the geometry it describes.

Geometry is applied in this order (identically for photos, previews and video):
  1. rotate (90° steps, clockwise) then flip
  2. straighten (small clockwise rotation, auto-zoomed so no empty corners show)
  3. crop (normalised rectangle inside the straightened frame)
  4. resize to the export resolution
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass

from .grading import HOUSE_GRADE, normalize_grade

STAMP_POSITIONS = ("br", "bl", "tr", "tl", "bc", "tc")

DEFAULT_EDIT: dict = {
    "rotate": 0,
    "flip_h": False,
    "flip_v": False,
    "straighten": 0.0,
    "aspect": "original",
    "crop": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
    "grade": normalize_grade(HOUSE_GRADE),  # "BRM Blue"
    "sharpen": 0,     # 0..100
    "vignette": 0,    # -100..100 (negative darkens edges)
    # Graduated "sky" filter: darkens/tints the top of the frame, fading out by `height` %.
    "grad": {"strength": 0, "height": 50, "color": "#000000"},
    "stamp": {
        "enabled": True,
        "position": "br",
        # Matches BRM Highlife's posts: solid black script, no shadow, tucked into the corner.
        "size": 24,       # stamp width as % of the image's short edge
        "margin": 2.7,    # % of the short edge
        "opacity": 95,
        "color": "#000000",
        "shadow": 0,      # drop-shadow strength 0..100
    },
    # Where the shot was taken, bottom-left, e.g. "Cowes, Australia" (filled in from the
    # photo/video GPS when it is added). Smaller than the BRM Highlife stamp; auto contrast.
    "location": {
        "enabled": True,
        "text": "",
        "position": "bl",
        "size": 2.2,      # text height as % of the image's short edge (BRM stamp is ~4%)
        "margin": 2.7,    # % of the short edge, same as the BRM stamp
        "opacity": 95,
        "color": "auto",
    },
    "video": {
        "trim_start": 0.0,
        "trim_end": None,  # None = end of clip
        "speed": 1.0,
        "mute": False,
        "stabilize": 0,   # 0..100 (vidstab smoothing; applied in playback & export)
        "denoise": 0,     # 0..100 (hqdn3d)
    },
}


def _num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    return min(max(v, lo), hi)


def normalize_edit(edit: dict | None) -> dict:
    e = copy.deepcopy(DEFAULT_EDIT)
    edit = edit or {}
    e["rotate"] = int(round(_num(edit.get("rotate", 0), -360, 360, 0) / 90)) * 90 % 360
    e["flip_h"] = bool(edit.get("flip_h", False))
    e["flip_v"] = bool(edit.get("flip_v", False))
    e["straighten"] = _num(edit.get("straighten", 0), -45, 45, 0.0)
    e["aspect"] = str(edit.get("aspect", "original"))
    c = edit.get("crop") or {}
    x = _num(c.get("x", 0), 0, 1, 0.0)
    y = _num(c.get("y", 0), 0, 1, 0.0)
    w = _num(c.get("w", 1), 0.01, 1, 1.0)
    h = _num(c.get("h", 1), 0.01, 1, 1.0)
    e["crop"] = {"x": min(x, 1 - w), "y": min(y, 1 - h), "w": w, "h": h}
    e["grade"] = normalize_grade(edit["grade"]) if "grade" in edit else e["grade"]
    e["sharpen"] = _num(edit.get("sharpen", 0), 0, 100, 0)
    e["vignette"] = _num(edit.get("vignette", 0), -100, 100, 0)
    gr = {**DEFAULT_EDIT["grad"], **(edit.get("grad") or {})}
    gcol = str(gr.get("color") or "#000000")
    e["grad"] = {"strength": _num(gr.get("strength"), 0, 100, 0),
                 "height": _num(gr.get("height"), 5, 100, 50),
                 "color": gcol if len(gcol) == 7 and gcol.startswith("#") else "#000000"}

    s = {**DEFAULT_EDIT["stamp"], **(edit.get("stamp") or {})}
    pos = s.get("position")
    color = str(s.get("color") or DEFAULT_EDIT["stamp"]["color"])
    if color != "auto" and not (len(color) == 7 and color.startswith("#")):
        color = DEFAULT_EDIT["stamp"]["color"]
    e["stamp"] = {
        "enabled": bool(s.get("enabled")),
        "position": pos if pos in STAMP_POSITIONS else "br",
        "size": _num(s.get("size"), 5, 90, 24),
        "margin": _num(s.get("margin"), 0, 20, 2.7),
        "opacity": _num(s.get("opacity"), 0, 100, 95),
        "color": color,
        "shadow": _num(s.get("shadow"), 0, 100, 0),
    }

    lo = {**DEFAULT_EDIT["location"], **(edit.get("location") or {})}
    lcol = str(lo.get("color") or "auto")
    e["location"] = {
        "enabled": bool(lo.get("enabled")),
        "text": " ".join(str(lo.get("text") or "").split())[:80],
        "position": lo.get("position") if lo.get("position") in STAMP_POSITIONS else "bl",
        "size": _num(lo.get("size"), 0.5, 15, 2.2),
        "margin": _num(lo.get("margin"), 0, 20, 2.7),
        "opacity": _num(lo.get("opacity"), 0, 100, 95),
        "color": lcol if lcol == "auto" or (len(lcol) == 7 and lcol.startswith("#")) else "auto",
    }

    v = {**DEFAULT_EDIT["video"], **(edit.get("video") or {})}
    start = _num(v.get("trim_start"), 0, 1e7, 0.0)
    end = v.get("trim_end")
    end = None if end in (None, "") else _num(end, 0, 1e7, None)
    if end is not None and end <= start:
        end = None
    e["video"] = {
        "trim_start": start,
        "trim_end": end,
        "speed": _num(v.get("speed"), 0.25, 4, 1.0),
        "mute": bool(v.get("mute")),
        "stabilize": _num(v.get("stabilize"), 0, 100, 0),
        "denoise": _num(v.get("denoise"), 0, 100, 0),
    }
    return e


@dataclass
class Geometry:
    """Pixel geometry for one source size + edit."""
    src_w: int
    src_h: int
    rotate: int        # clockwise degrees, multiple of 90
    flip_h: bool
    flip_v: bool
    angle: float       # straighten, degrees clockwise
    ow: int            # oriented size (after 90° rotation)
    oh: int
    fw: float          # straightened frame size (inner rectangle)
    fh: float
    cx: float          # crop rect inside the frame, pixels
    cy: float
    cw: float
    ch: float

    @property
    def frame_offset(self) -> tuple[float, float]:
        """Top-left of the straightened frame inside the rotated (oriented) canvas."""
        return (self.ow - self.fw) / 2, (self.oh - self.fh) / 2

    def output_size(self, long_edge: int | tuple | None = None, even: bool = False,
                    allow_upscale: bool = True) -> tuple[int, int]:
        """Output pixels. `long_edge` is a long-edge length, or a (max_w, max_h) box
        (either may be None) that the crop is fitted inside, keeping its shape."""
        w, h = self.cw, self.ch
        if isinstance(long_edge, (tuple, list)):
            bw, bh = long_edge
            ks = [b / d for b, d in ((bw, w), (bh, h)) if b]
            k = min(ks) if ks else 1.0
        else:
            k = long_edge / max(w, h) if long_edge else 1.0
        if k < 1 or allow_upscale:
            w, h = w * k, h * k
        w, h = max(int(round(w)), 2), max(int(round(h)), 2)
        if even:
            w, h = w - w % 2, h - h % 2
        return w, h


def straighten_scale(w: float, h: float, angle_deg: float) -> float:
    """Largest scale s so a centred (s*w, s*h) rect fits inside the w×h image rotated by angle."""
    a = math.radians(abs(angle_deg))
    if a == 0:
        return 1.0
    c, s = math.cos(a), math.sin(a)
    return min(w / (w * c + h * s), h / (w * s + h * c))


def plan_geometry(src_w: int, src_h: int, edit: dict) -> Geometry:
    rot = edit["rotate"] % 360
    ow, oh = (src_h, src_w) if rot in (90, 270) else (src_w, src_h)
    k = straighten_scale(ow, oh, edit["straighten"])
    fw, fh = ow * k, oh * k
    c = edit["crop"]
    return Geometry(src_w, src_h, rot, edit["flip_h"], edit["flip_v"], edit["straighten"],
                    ow, oh, fw, fh, c["x"] * fw, c["y"] * fh, c["w"] * fw, c["h"] * fh)


def has_geometry_change(edit: dict) -> bool:
    c = edit["crop"]
    return bool(edit["rotate"] or edit["flip_h"] or edit["flip_v"] or edit["straighten"]
                or c["x"] or c["y"] or c["w"] < 1 or c["h"] < 1)
