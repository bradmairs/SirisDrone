"""One-click export recipes and the automatic edit used by them and the watch folder."""
from __future__ import annotations

from . import analysis, geo, grading
from .edits import normalize_edit, plan_geometry

PANORAMA_ASPECT = 2.0  # wider than 2:1 → Instagram carousel instead of a single post
# Shapes Instagram shows uncropped in the feed: 4:5 portrait up to 1.91:1 landscape.
IG_TALLEST, IG_WIDEST = 4 / 5, 1.91

RECIPES: dict[str, dict] = {
    "instagram": {
        "label": "Instagram post",
        "kinds": ["photo"],
        "help": "Keeps the photo's orientation: landscape stays landscape (1080 wide, cropped only "
                "past 1.91:1), portrait is cropped to 4:5 (1080 × 1350). JPEG, stamp, GPS removed. "
                "Panoramas become a carousel.",
        "options": {"photo_format": "jpeg", "jpeg_quality": 92, "photo_size": "1080x1350",
                    "keep_metadata": True, "strip_gps": True,
                    "name_pattern": "{date} {place} {name} IG"},
    },
    "carousel": {
        "label": "Instagram carousel",
        "kinds": ["photo"],
        "help": "Splits a panorama into seamless 1080 × 1350 slides; stamp on the last slide.",
        "options": {"photo_format": "jpeg", "jpeg_quality": 92, "keep_metadata": True,
                    "strip_gps": True, "name_pattern": "{date} {place} {name} IG"},
    },
    "appletv": {
        "label": "Apple TV 4K",
        "kinds": ["video"],
        "help": "3840 × 2160 HEVC 10-bit at the source frame rate (max 60), maximum quality.",
        "options": {"video_codec": "h265", "video_quality": "max", "video_size": "3840x2160",
                    "video_fps": "max60", "keep_metadata": True, "strip_gps": False,
                    "name_pattern": "{date} {place} {name} ATV"},
    },
    "full": {
        "label": "Full resolution",
        "kinds": ["photo", "video"],
        "help": "Original resolution and frame rate, maximum quality JPEG / HEVC 10-bit.",
        "options": {"photo_format": "jpeg", "jpeg_quality": 95, "photo_size": "original",
                    "video_codec": "h265", "video_quality": "max", "video_size": "original",
                    "video_fps": "source", "keep_metadata": True, "strip_gps": False,
                    "name_pattern": "{name}_BRM"},
    },
}

D_CINELIKE = "D-Cinelike → BRM Blue"


def frame_aspect(item: dict, edit: dict) -> float:
    g = plan_geometry(item["width"], item["height"],
                      normalize_edit({**edit, "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}))
    return g.fw / g.fh


def is_panorama(item: dict, edit: dict) -> bool:
    return item["kind"] == "photo" and frame_aspect(item, edit) >= PANORAMA_ASPECT


def aspect_crop(item: dict, edit: dict, ratio: float) -> dict:
    """Centred crop of the given width/height ratio inside the straightened frame."""
    rn = ratio / frame_aspect(item, edit)
    w, h = 1.0, 1.0 / rn
    if h > 1:
        w, h = rn, 1.0
    return {"x": (1 - w) / 2, "y": (1 - h) / 2, "w": w, "h": h}


def resolve(recipe: str, item: dict, edit: dict) -> str:
    """"instagram" turns into "carousel" for panoramas."""
    return "carousel" if recipe == "instagram" and is_panorama(item, edit) else recipe


def instagram_crop(item: dict, edit: dict) -> dict:
    """Keep the photo's own orientation and shape; only crop what Instagram would cut off.
    A crop the user already chose is kept when Instagram can show it as is."""
    fa = frame_aspect(item, edit)
    c = edit["crop"]
    if edit["aspect"] != "original" and IG_TALLEST - 0.01 <= fa * c["w"] / c["h"] <= IG_WIDEST + 0.01:
        return edit
    if fa < IG_TALLEST:
        return {**edit, "aspect": "4:5", "crop": aspect_crop(item, edit, IG_TALLEST)}
    if fa > IG_WIDEST:
        return {**edit, "aspect": "free", "crop": aspect_crop(item, edit, IG_WIDEST)}
    return {**edit, "aspect": "original", "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}


def recipe_edit(recipe: str, item: dict, edit: dict) -> dict:
    """The edit a recipe needs (e.g. Instagram's shape limits), keeping the user's own crop
    when it already fits."""
    edit = normalize_edit(edit)
    if recipe == "instagram":
        edit = instagram_crop(item, edit)
    elif recipe == "carousel" and edit["aspect"] in ("4:5", "1:1", "9:16"):
        edit = {**edit, "aspect": "original", "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}
    return normalize_edit(edit)


TOP_DOWN_PITCH = -55.0  # camera pointing further down than this: no horizon to level


def auto_edit(item: dict, edit: dict, source, horizon: bool = True, exposure: bool = True,
              path=None) -> dict:
    """Automatic first pass: D-Cinelike look for 10-bit Mini 3 Pro video, level the horizon,
    set exposure. `source` is a preview-sized image of the item (photo or video frame);
    `path` lets it read the gimbal angle so straight-down shots are never "levelled"."""
    edit = normalize_edit(edit)
    if horizon and path is not None:
        pitch = geo.gimbal_pitch(path)
        if pitch is not None and pitch < TOP_DOWN_PITCH:
            horizon = False
    if item["kind"] == "video" and item.get("bit_depth", 8) >= 10:
        # The Mini 3 Pro records D-Cinelike in 10-bit (Normal colour is 8-bit).
        edit["grade"] = grading.normalize_grade(grading.PRESETS[D_CINELIKE])
    if horizon:
        angle = analysis.auto_straighten(source, edit)
        if angle is not None and abs(angle) >= 0.2:
            edit["straighten"] = angle
    if exposure:
        edit["grade"]["exposure"] = analysis.auto_exposure(source, edit)
    return normalize_edit(edit)
