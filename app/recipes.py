"""One-click export recipes and the automatic edit used by them and the watch folder."""
from __future__ import annotations

from . import analysis, geo, grading
from .edits import normalize_edit

RECIPES: dict[str, dict] = {
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


def recipe_edit(recipe: str, item: dict, edit: dict) -> dict:
    """The edit a recipe needs. The current recipes keep the user's own edit as it is."""
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
    if edit["stamp"]["color"].lower() in ("#000000", "#ffffff"):
        edit["stamp"]["color"] = "auto"  # black or white, whichever stands out on this shot
    return normalize_edit(edit)
