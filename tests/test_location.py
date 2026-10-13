import numpy as np
from PIL import Image

from app import geo, render
from app.edits import DEFAULT_EDIT, normalize_edit
from app.stamp import location_mask, location_settings, render_stamp
from tests.test_api import upload
from tests.test_auto import horizon_scene
from tests.test_stamp_auto import MASK


def test_city_country_from_gps(monkeypatch, tmp_path):
    geo.configure(tmp_path / "geo.json")
    calls = []
    monkeypatch.setattr(geo, "_lookup_address", lambda lat, lon: calls.append(1) or
                        {"town": "Cowes", "county": "Bass Coast Shire", "state": "Victoria", "country": "Australia"})
    assert geo.city_country((-38.45, 145.24)) == "Cowes, Australia"
    assert geo.city_country((-38.4501, 145.2402)) == "Cowes, Australia" and len(calls) == 1  # cached
    monkeypatch.setattr(geo, "_lookup_address", lambda lat, lon: {"village": "Kyneton", "country": "Australia"})
    assert geo.city_country((-37.2, 144.4)) == "Kyneton, Australia"
    assert geo.city_country(None) == ""
    # Over the sea only the state is known at the spot itself: the nearest town 2 km away wins.
    seen = []

    def coast(lat, lon):
        seen.append((round(lat, 2), round(lon, 2)))
        if len(seen) < 3:
            return {"state": "Victoria", "country": "Australia"}
        return {"village": "Peterborough", "state": "Victoria", "country": "Australia"}
    monkeypatch.setattr(geo, "_lookup_address", coast)
    assert geo.city_country((-38.60, 142.70)) == "Peterborough, Australia" and len(seen) == 3
    monkeypatch.setattr(geo, "_lookup_address", lambda lat, lon: {"state": "Victoria", "country": "Australia"})
    assert geo.city_country((-39.0, 143.0)) == "Victoria, Australia"  # nothing nearby: the state


def test_location_text_is_smaller_than_the_brm_stamp():
    edit = normalize_edit({**DEFAULT_EDIT, "location": {"text": "Cowes, Australia"}})
    loc = edit["location"]
    m = location_mask(loc["text"])
    text_img, x, y = render_stamp(m, 1600, 1200, location_settings(loc, m))
    brm_img, bx, by = render_stamp(MASK, 1600, 1200, edit["stamp"])
    assert text_img.height < brm_img.height * 0.65
    assert x < 100 and y > 1000 and bx > 1000                   # bottom-left vs bottom-right


def test_location_drawn_bottom_left_with_contrast():
    def corner_after(value):
        src = render.from_array(np.full((800, 1200, 3), value, np.uint8))
        edit = normalize_edit({**DEFAULT_EDIT, "grade": {}, "stamp": {"enabled": False},
                               "location": {"text": "Cowes, Australia"}})
        out = render.render(src, edit, stamp_mask=MASK)
        return out[740:790, 20:400]
    dark, light = corner_after(20), corner_after(235)
    assert dark.max() > 0.8                                     # white text on a dark corner
    assert light.min() < 0.2                                    # black text on a light corner
    off = normalize_edit({**DEFAULT_EDIT, "grade": {}, "stamp": {"enabled": False},
                          "location": {"text": "Cowes, Australia", "enabled": False}})
    plain = render.render(render.from_array(np.full((800, 1200, 3), 20, np.uint8)), off, stamp_mask=MASK)
    assert plain[740:790, 20:400].max() < 0.2


def test_import_fills_location_from_gps(client, tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_lookup_address", lambda lat, lon: {"city": "Takaka", "country": "New Zealand"})
    item = upload(client, horizon_scene(tmp_path / "DJI_0900.JPG", 0, gps=(-40.85, 172.80)))
    assert client.get(f"/api/media/{item['id']}/edit").json()["location"]["text"] == "Takaka, New Zealand"
    nogps = upload(client, horizon_scene(tmp_path / "DJI_0901.JPG", 0))
    assert client.get(f"/api/media/{nogps['id']}/edit").json()["location"]["text"] == ""
    assert client.post(f"/api/media/{nogps['id']}/location").status_code == 404


def test_video_graph_has_location_overlay(tmp_path):
    from app import video
    meta = {"width": 640, "height": 360, "duration": 1, "color_space": None}
    edit = normalize_edit({**DEFAULT_EDIT, "location": {"text": "Cowes, Australia", "color": "#ffffff"}})
    g = video.build_graph(meta, edit, tmp_path, MASK)
    assert "[location]" in g["filter"] and (tmp_path / "location.png").exists()
    edit["location"]["enabled"] = False
    assert "[location]" not in video.build_graph(meta, edit, tmp_path, MASK)["filter"]
