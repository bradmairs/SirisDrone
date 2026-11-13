"""Release E: flight data overlay, photo noise reduction, edit history, phone upload."""
import numpy as np
import pytest
from PIL import Image

from app import flightdata, render, telemetry, video
from app.edits import normalize_edit
from tests.conftest import make_video
from tests.scenes import scene
from tests.test_trips import mini3_srt


def upload_files(client, *paths):
    files = [("files", (p.name, open(p, "rb"), "application/octet-stream")) for p in paths]
    r = client.post("/api/media/upload", files=files)
    assert r.status_code == 200, r.text
    return r.json()


def test_flight_values_and_units():
    row = {"alt": 52.4, "speed": 10.0, "dist": 1234.0, "vspeed": 1.5, "iso": 100, "shutter": "1/1000.0",
           "fnum": 1.7, "lat": -38.5, "lon": 145.25, "time": "2026-04-03 10:12:31.123"}
    s = flightdata.normalize({"items": ["alt", "speed", "dist", "camera", "time", "bogus"]})
    assert s["items"] == ["alt", "speed", "dist", "camera", "time"]
    assert flightdata.values(row, s) == [("ALT", "52 m"), ("SPEED", "36 km/h"), ("DIST", "1.2 km"),
                                         ("CAMERA", "ISO 100  1/1000  f/1.7"), ("TIME", "10:12:31")]
    imp = {**s, "units": "imperial", "items": ["alt", "speed", "dist"]}
    assert flightdata.values(row, imp) == [("ALT", "172 ft"), ("SPEED", "22 mph"), ("DIST", "4049 ft")]
    assert flightdata.values({}, s)[0] == ("ALT", "–")
    a = flightdata.render_box(flightdata.values(row, s), 3840, 2160, s)
    b = flightdata.render_box(flightdata.values({}, s), 3840, 2160, s)
    assert a.size == b.size and a.mode == "RGBA"              # the box never changes size


def test_uploaded_srt_is_kept_with_its_clip_and_used_for_overlay_and_map(client, tmp_path):
    clip = make_video(tmp_path / "DJI_0040.MP4", size="640x360", seconds=3, audio=False)
    srt = tmp_path / "DJI_0040.SRT"
    srt.write_text(mini3_srt(seconds=3))
    r = upload_files(client, srt, clip)                       # any order
    vid = r["added"][0]["id"]
    assert r["flight"] == [vid] and not r["errors"]
    fd = client.get(f"/api/media/{vid}/flight").json()
    assert fd["available"] and fd["summary"]["max_alt"] == pytest.approx(56.0) and len(fd["samples"]) == 7
    assert any(s["id"] == vid for s in client.get("/api/map").json()["shots"])
    lone = tmp_path / "DJI_9999.SRT"
    lone.write_text(mini3_srt(1))
    assert "add the video" in upload_files(client, lone)["errors"][0]

    # preview with the overlay differs from without, in the top-left corner only
    edit = client.get(f"/api/media/{vid}/edit").json()
    edit["stamp"]["enabled"] = edit["location"]["enabled"] = False
    plain = client.post(f"/api/media/{vid}/preview", json={"edit": edit, "t": 1, "size": 640}).content
    edit["flight"] = {"enabled": True}
    hud = client.post(f"/api/media/{vid}/preview", json={"edit": edit, "t": 1, "size": 640}).content
    import io
    a = np.asarray(Image.open(io.BytesIO(plain)), np.float32)
    b = np.asarray(Image.open(io.BytesIO(hud)), np.float32)
    diff = np.abs(a - b).mean(axis=2) > 30
    ys, xs = np.nonzero(diff)
    assert diff.sum() > 200 and xs.max() < 320 and ys.max() < 120


def test_video_export_burns_in_flight_data(client, env, tmp_path):
    _, out, _ = env
    clip = make_video(tmp_path / "DJI_0041.MP4", size="640x360", seconds=3, audio=False)
    srt = tmp_path / "DJI_0041.SRT"
    srt.write_text(mini3_srt(seconds=3))
    vid = upload_files(client, clip, srt)["added"][0]["id"]
    edit = client.get(f"/api/media/{vid}/edit").json()
    edit["stamp"]["enabled"] = edit["location"]["enabled"] = False
    client.put(f"/api/media/{vid}/edit", json=edit)
    opts = {"video_codec": "h264", "video_size": "original", "root": "NAS", "folder": "plain",
            "name_pattern": "{name}"}
    client.post("/api/export", json={"media_ids": [vid], "options": opts})
    edit["flight"] = {"enabled": True, "items": ["alt", "speed"], "size": 5}
    client.put(f"/api/media/{vid}/edit", json=edit)
    client.post("/api/export", json={"media_ids": [vid], "options": {**opts, "folder": "hud"}})
    client.app_state.jobs.wait_idle(300)
    assert all(j["status"] == "done" for j in client.get("/api/jobs").json())
    meta = video.probe(out / "plain" / "DJI_0041.mp4")
    a = video.extract_frame(out / "plain" / "DJI_0041.mp4", meta, 2.2).astype(np.float32)
    b = video.extract_frame(out / "hud" / "DJI_0041.mp4", meta, 2.2).astype(np.float32)
    diff = np.abs(a - b).mean(axis=2) > 30
    ys, xs = np.nonzero(diff)
    assert diff.sum() > 400 and xs.max() < 400 and ys.max() < 120   # a HUD in the top-left only


def test_photo_noise_reduction_cleans_flat_areas_and_keeps_edges():
    rng = np.random.default_rng(0)
    clean = np.asarray(scene(3, 800, 600), np.float32) / 255
    noisy = np.clip(clean + rng.normal(0, 0.04, clean.shape).astype(np.float32), 0, 1)
    out = render.apply_denoise(noisy.copy(), 80)
    err = lambda x: float(np.sqrt(((x - clean) ** 2).mean()))
    assert err(out) < err(noisy) * 0.7
    gx = lambda x: np.abs(np.diff(x.mean(-1), axis=1))
    edges = gx(clean) > 0.1
    assert (gx(out) * edges).sum() > 0.75 * (gx(clean) * edges).sum()
    assert render.apply_denoise(noisy.copy(), 0) is not None
    e = normalize_edit({"noise": 250})
    assert e["noise"] == 100 and normalize_edit({})["noise"] == 0


def test_noise_reduction_in_strips_matches_whole_image(monkeypatch):
    rng = np.random.default_rng(1)
    img = np.clip(np.asarray(scene(4, 640, 900), np.float32) / 255
                  + rng.normal(0, 0.04, (900, 640, 3)).astype(np.float32), 0, 1)
    whole = img.copy()
    monkeypatch.setattr(render, "NOISE_STRIP", 10000)
    render.apply_denoise(whole, 70)
    monkeypatch.setattr(render, "NOISE_STRIP", 128)
    strips = img.copy()
    render.apply_denoise(strips, 70)
    assert np.abs(whole - strips).max() < 2e-3


def test_edit_history_records_versions_and_restores(client, tmp_path):
    from tests.test_api import upload
    p = tmp_path / "a.jpg"
    scene(1, 400, 300).save(p)
    mid = upload(client, p)["id"]
    lib = client.app_state.lib
    edit = client.get(f"/api/media/{mid}/edit").json()
    for ev in (0.2, 0.4, 0.6):            # a slider drag: one version
        edit["grade"]["exposure"] = ev
        client.put(f"/api/media/{mid}/edit", json=edit)
    client.put(f"/api/media/{mid}/edit", json=edit)             # unchanged: no new version
    lib.set_edit(mid, {**edit, "sharpen": 40}, "Matched colours")
    h = client.get(f"/api/media/{mid}/history").json()
    assert [x["label"] for x in h] == ["Matched colours", "Edited", "Added"]
    assert [x["index"] for x in h] == [2, 1, 0]
    restored = client.post(f"/api/media/{mid}/history/0/restore").json()
    assert restored["grade"]["exposure"] == normalize_edit({})["grade"]["exposure"]
    assert client.get(f"/api/media/{mid}/edit").json() == restored
    assert client.get(f"/api/media/{mid}/history").json()[0]["label"].startswith("Restored “Added”")
    assert client.post(f"/api/media/{mid}/history/99/restore").status_code == 404


def test_history_is_capped_but_keeps_the_first_version(client, tmp_path):
    from tests.test_api import upload
    p = tmp_path / "b.jpg"
    scene(2, 400, 300).save(p)
    mid = upload(client, p)["id"]
    lib = client.app_state.lib
    for i in range(lib.HISTORY_MAX + 10):
        lib.set_edit(mid, {"sharpen": i + 1}, f"step {i}")
    h = lib.history(mid)
    assert len(h) == lib.HISTORY_MAX and h[0]["label"] == "Added" and h[-1]["label"] == f"step {lib.HISTORY_MAX + 9}"


def test_phone_upload_page_share_target_and_manifest(client, tmp_path):
    import json
    page = client.get("/upload")
    assert page.status_code == 200 and "Choose photos" in page.text
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert sw.headers["service-worker-allowed"] == "/"
    man = json.loads(client.get("/static/manifest.webmanifest").text)
    assert man["share_target"]["action"] == "/share" and man["share_target"]["params"]["files"][0]["name"] == "files"
    assert all(client.get(i["src"]).status_code == 200 for i in man["icons"])
    p = tmp_path / "phone.jpg"
    scene(5, 400, 300).save(p)
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    r = client.post("/share", files=[("files", (p.name, open(p, "rb"), "image/jpeg")),
                                     ("files", (bad.name, open(bad, "rb"), "text/plain"))],
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/upload?shared=1&errors=1"
    assert [i["name"] for i in client.get("/api/media").json()] == ["phone.jpg"]
    assert client.post("/share", follow_redirects=False).headers["location"] == "/upload?shared=0&errors=0"
