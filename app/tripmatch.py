"""Trip colour match: make a trip's shots look like one set.

Drone exposure and white balance drift through a flight (clouds, sun angle, auto WB), so after
each shot's own automatic edit, clips cut together or photos posted side by side can jump in
brightness and colour. For each shot we render a small preview with its current edit and
measure its brightness (median luminance) and colour balance (red/blue and green/magenta, in
linear light). Each shot is then nudged most of the way towards the trip's median, using its
own Exposure, Temperature and Tint sliders, so it stays fully editable.

Photos and videos are matched separately (they start from different looks), and corrections
are limited so a sunset stays a sunset: at most ±0.6 EV, ±25 temperature and ±12 tint.
"""
from __future__ import annotations

import numpy as np

from . import grading, render
from .edits import normalize_edit

STRENGTH = 0.75          # how far towards the trip median each shot moves
MAX_EV, MAX_TEMP, MAX_TINT = 0.6, 25.0, 12.0
MIN_EV, MIN_TEMP, MIN_TINT = 0.04, 1.5, 1.5   # smaller corrections aren't worth changing a slider
PREVIEW = 256

# grade_pixels' white balance, per slider unit (see grading.grade_pixels step 2):
TEMP_RB = 0.64 / 100     # log2(R/B) change per temperature unit
TINT_GM = -0.28 / 100    # log2(G / sqrt(R·B)) change per tint unit


def _plain(edit: dict) -> dict:
    """The edit without anything that isn't the overall look (stamp, sky filter, vignette)."""
    return {**edit, "stamp": {"enabled": False}, "location": {"enabled": False},
            "vignette": 0, "grad": {"strength": 0}, "sharpen": 0}


def measure(src, edit: dict) -> dict:
    """Brightness and colour balance of a shot rendered with `edit`."""
    rgb = render.render(src, _plain(normalize_edit(edit)), long_edge=PREVIEW, fast=True)
    lin = grading._srgb_to_linear(np.clip(rgb.reshape(-1, 3), 0, 1))
    luma = lin @ grading.LUMA
    mid = (luma > 0.01) & (luma < 0.85)        # ignore crushed shadows and clipped sky
    use = lin[mid] if mid.sum() > 50 else lin
    m = use.mean(axis=0) + 1e-4
    return {"ev": float(np.log2(np.median(luma) + 1e-4)),
            "rb": float(np.log2(m[0] / m[2])),
            "gm": float(np.log2(m[1] / np.sqrt(m[0] * m[2])))}


def correction(stats: dict, target: dict) -> dict:
    """Slider changes that move a shot with `stats` most of the way to `target`."""
    d_ev = float(np.clip(STRENGTH * (target["ev"] - stats["ev"]), -MAX_EV, MAX_EV))
    d_t = float(np.clip(STRENGTH * (target["rb"] - stats["rb"]) / TEMP_RB, -MAX_TEMP, MAX_TEMP))
    d_tn = float(np.clip(STRENGTH * (target["gm"] - stats["gm"]) / TINT_GM, -MAX_TINT, MAX_TINT))
    return {"exposure": d_ev if abs(d_ev) >= MIN_EV else 0.0,
            "temperature": d_t if abs(d_t) >= MIN_TEMP else 0.0,
            "tint": d_tn if abs(d_tn) >= MIN_TINT else 0.0}


_DIMS = (("exposure", "ev"), ("temperature", "rb"), ("tint", "gm"))


def refine(src, edit: dict, stats: dict, target: dict) -> dict:
    """Correction for one shot. The first guess uses the white-balance maths alone; the grade
    after it (contrast, BRM Blue's saturation) changes how much a slider really moves the
    result, so the effect of that guess is measured and the step rescaled (one secant step)."""
    first = correction(stats, target)
    if not any(first.values()):
        return first
    after = measure(src, apply(edit, first))
    out = dict(first)
    for slider, stat in _DIMS:
        moved = after[stat] - stats[stat]
        if first[slider] and abs(moved) > 1e-3 and moved * first[slider] * {"ev": 1, "rb": 1, "gm": -1}[stat] > 0:
            out[slider] = STRENGTH * (target[stat] - stats[stat]) * first[slider] / moved
    return limit(out)


def limit(corr: dict) -> dict:
    return {"exposure": float(np.clip(corr["exposure"], -MAX_EV, MAX_EV)),
            "temperature": float(np.clip(corr["temperature"], -MAX_TEMP, MAX_TEMP)),
            "tint": float(np.clip(corr["tint"], -MAX_TINT, MAX_TINT))}


def apply(edit: dict, corr: dict) -> dict:
    e = normalize_edit(edit)
    g = dict(e["grade"])
    g["exposure"] = round(float(np.clip(g["exposure"] + corr["exposure"], -3, 3)), 2)
    g["temperature"] = round(float(np.clip(g["temperature"] + corr["temperature"], -100, 100)))
    g["tint"] = round(float(np.clip(g["tint"] + corr["tint"], -100, 100)))
    e["grade"] = grading.normalize_grade(g)
    return e


def target_of(stats: list[dict]) -> dict:
    return {k: float(np.median([s[k] for s in stats])) for k in ("ev", "rb", "gm")}


def _frame_time(item: dict, edit: dict) -> float:
    v = edit["video"]
    end = v["trim_end"] if v["trim_end"] is not None else (item.get("duration") or 0)
    return max((v["trim_start"] + end) / 2, 0.0)


def match_library(lib, items: list[dict], reference: list[dict] | None = None) -> dict[str, dict]:
    """Match `items` trip by trip (and photos/videos separately). The target is the median of
    `items` plus `reference` (other shots of the same trips, left unchanged). Saves the new
    edits and returns {id: {"exposure", "temperature", "tint"}} for the shots that changed."""
    groups: dict[tuple, list[tuple[dict, bool]]] = {}
    for it in items:
        groups.setdefault((lib.trip_key(it), it["kind"]), []).append((it, True))
    ids = {i["id"] for i in items}
    for it in reference or []:
        key = (lib.trip_key(it), it["kind"])
        if key in groups and it["id"] not in ids:
            groups[key].append((it, False))
    changed: dict[str, dict] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        measured = []
        for it, adjust in members:
            edit = normalize_edit(lib.get_edit(it["id"]))
            full = {**edit, "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}
            src = lib.preview_source(it, full, PREVIEW * 2, _frame_time(it, edit))
            measured.append((it, adjust, edit, src, measure(src, edit)))
        target = target_of([m[4] for m in measured])
        for it, adjust, edit, src, stats in measured:
            if not adjust:
                continue
            new = apply(edit, refine(src, edit, stats, target))
            delta = {k: round(new["grade"][k] - edit["grade"][k], 2)
                     for k in ("exposure", "temperature", "tint")}
            if any(delta.values()):
                lib.set_edit(it["id"], new)
                changed[it["id"]] = delta
    return changed
