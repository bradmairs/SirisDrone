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
    text_img, x, y = render_stamp(m, 1600, 1200, {**location_settings(loc, m), "shadow": 0})
    brm_img, bx, by = render_stamp(MASK, 1600, 1200, edit["stamp"])
    assert text_img.height < brm_img.height * 0.65
    assert x < 100 and y > 1000 and bx > 1000                   # bottom-left vs bottom-right
    halo = np.asarray(render_stamp(m, 1600, 1200, location_settings(loc, m))[0])
    assert loc["shadow"] > 0 and halo[..., 3].max() > 200 and halo[..., :3].max() == 255  # white halo


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


def test_location_style_bar_bold_town_lighter_country():
    m = np.asarray(location_mask("Cape Woolamai, Australia"))
    left = m[:, :40]
    assert (left == 255).sum() > 200                            # accent bar at the start
    values = set(np.unique(m).tolist())
    assert 255 in values and round(255 * 0.7) in values         # bold town, lighter country
    assert location_mask("Cape Woolamai, Australia").height == location_mask("Wye, UK").height
    assert location_mask("Somewhere") is not None              # no country is fine too


def test_video_without_gps_borrows_nearest_photo(tmp_path):
    trip = tmp_path / "2026-04-03_Philip Island"
    (trip / "JPEG").mkdir(parents=True)
    (trip / "Videos").mkdir()
    horizon_scene(trip / "JPEG" / "DJI_0607.JPG", 0, 400, 300, gps=(-38.52, 145.30))
    horizon_scene(trip / "JPEG" / "DJI_0700.JPG", 0, 400, 300, gps=(-37.00, 144.00))
    vid = trip / "Videos" / "DJI_0608.MP4"
    vid.write_bytes(b"no gps in here")
    gps = geo.gps_for_video(vid, {})
    assert gps and abs(gps[0] + 38.52) < 0.01 and abs(gps[1] - 145.30) < 0.01
    lone = tmp_path / "DJI_0900.MP4"
    lone.write_bytes(b"x")
    assert geo.gps_for_video(lone, {}) is None
