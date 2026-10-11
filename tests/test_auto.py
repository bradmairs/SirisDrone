import json
import subprocess

import numpy as np
from PIL import Image

from app import analysis, geo, render
from tests.conftest import make_video
from tests.test_api import upload


def horizon_scene(path, tilt, w=1600, h=1200, gps=None):
    rng = np.random.default_rng(1)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    hy = h * 0.42 + (x - w / 2) * np.tan(np.radians(tilt))
    sky = np.stack([0.5 + 0 * x, 0.7 + 0 * x, 0.95 - 0.1 * y / h], -1)
    sea = np.stack([0.1 + 0 * x, 0.3 + 0 * x, 0.5 + 0 * x], -1) + rng.normal(0, 0.04, (h, w, 1))
    img = (np.clip(np.where((y > hy)[..., None], sea, sky), 0, 1) * 255).astype(np.uint8)
    exif = Image.Exif()
    exif[271] = "DJI"
    if gps:
        exif[0x8825] = {1: "S" if gps[0] < 0 else "N", 2: (abs(gps[0]), 0.0, 0.0),
                        3: "E" if gps[1] > 0 else "W", 4: (abs(gps[1]), 0.0, 0.0)}
    Image.fromarray(img).save(path, quality=95, exif=exif)
    return path


def test_auto_horizon_levels_the_shot(tmp_path):
    p = horizon_scene(tmp_path / "t.jpg", 4.0)
    src = Image.open(p).convert("RGB")
    angle = analysis.auto_straighten(src, {})
    assert abs(angle + 4.0) <= 0.2
    levelled = render.render(src, {"straighten": angle, "grade": {}, "stamp": {"enabled": False}})
    tilt, _ = analysis.detect_tilt(Image.fromarray(render.to_uint8(levelled)))
    assert abs(tilt) <= 0.3
    # no horizon (straight-down texture) → no change suggested
    tex = Image.fromarray((np.random.default_rng(2).random((900, 1200, 3)) * 255).astype(np.uint8))
    assert analysis.auto_straighten(tex, {}) is None


def test_auto_exposure_brightens_dark_and_respects_highlights(tmp_path):
    p = horizon_scene(tmp_path / "t.jpg", 0)
    src = np.asarray(Image.open(p).convert("RGB"), np.float32)
    dark = Image.fromarray((src * 0.45).astype(np.uint8))
    assert analysis.auto_exposure(dark, {"grade": {}}) > 0.8
    assert abs(analysis.auto_exposure(Image.fromarray(src.astype(np.uint8)), {"grade": {}})) <= 0.3


def test_auto_endpoint_and_instagram_quick_export(client, env, tmp_path, monkeypatch):
    _, out, _ = env
    monkeypatch.setattr(geo, "_lookup", lambda lat, lon: "Wharariki Beach")
    item = upload(client, horizon_scene(tmp_path / "DJI_0100.JPG", 3.0, gps=(-40.5, 172.67)))
    r = client.post(f"/api/media/{item['id']}/auto", json={}).json()
    assert r["horizon_found"] and abs(r["straighten"] + 3.0) <= 0.2 and "exposure" in r

    res = client.post("/api/quick-export", json={"recipe": "instagram", "media_ids": [item["id"]],
                                                  "root": "NAS", "folder": "insta"}).json()
    assert res["jobs"] and not res["skipped"]
    client.app_state.jobs.wait_idle()
    files = list((out / "insta").glob("*.jpg"))
    assert len(files) == 1, files
    assert files[0].name.endswith("Wharariki Beach DJI_0100 IG.jpg")
    im = Image.open(files[0])
    assert im.width == 1080 and abs(im.width / im.height - 4 / 3) < 0.02  # stays landscape
    assert 0x8825 not in im.getexif()                       # GPS stripped for social
    assert client.get(f"/api/media/{item['id']}/edit").json()["aspect"] == "original"


def test_instagram_keeps_orientation(client, env, tmp_path):
    _, out, _ = env
    cases = {"PORT_0001.JPG": (900, 1600), "WIDE_0002.JPG": (1900, 1000), "SQR_0003.JPG": (1000, 1000)}
    ids = [upload(client, horizon_scene(tmp_path / n, 0, w=w, h=h))["id"] for n, (w, h) in cases.items()]
    client.post("/api/quick-export", json={"recipe": "instagram", "media_ids": ids,
                                           "root": "NAS", "folder": "ig"})
    client.app_state.jobs.wait_idle()
    size = {f.name.split(" IG")[0].split(" ")[-1]: Image.open(f).size for f in (out / "ig").glob("*.jpg")}
    assert size["PORT_0001"] == (1080, 1350)                 # too tall: cropped to 4:5
    assert size["WIDE_0002"][0] == 1080 and abs(1080 / size["WIDE_0002"][1] - 1.91) < 0.02
    assert size["SQR_0003"] == (1080, 1080)


def test_panorama_becomes_carousel(client, env, tmp_path):
    _, out, _ = env
    pano = upload(client, horizon_scene(tmp_path / "PANO_0001.JPG", 0, w=4000, h=1000))
    client.post("/api/quick-export", json={"recipe": "instagram", "media_ids": [pano["id"]],
                                           "root": "NAS", "folder": "pano"})
    client.app_state.jobs.wait_idle()
    job = client.get("/api/jobs").json()[-1]
    assert job["kind"] == "carousel" and job["status"] == "done", job
    slides = sorted((out / "pano").glob("*.jpg"))
    assert len(slides) == 5                                  # 4:1 pano ≈ 5 × 4:5 slides
    assert all(Image.open(s).size == (1080, 1350) for s in slides)
    last = np.asarray(Image.open(slides[-1]), np.float32)
    first = np.asarray(Image.open(slides[0]), np.float32)
    corner = (slice(1250, 1330), slice(780, 1060))
    ink = lambda a: int((a[corner].max(axis=-1) < 40).sum())
    assert ink(last) > 500 and ink(first) == 0               # black stamp only on the last slide
    # seamless: the edge columns of neighbouring slides continue each other
    a, b = np.asarray(Image.open(slides[1]), np.float32), np.asarray(Image.open(slides[2]), np.float32)
    assert np.abs(a[:, -1] - b[:, 0]).mean() < 6


def test_apple_tv_quick_export_skips_photos(client, env, tmp_path):
    _, out, raw = env
    make_video(raw / "DJI_0200.MP4", size="320x180", seconds=1)
    vid = client.post("/api/media/import", json={"root": "Footage", "path": ""}).json()["added"][0]
    photo = upload(client, horizon_scene(tmp_path / "p.jpg", 0, 400, 300))
    res = client.post("/api/quick-export", json={"recipe": "appletv", "media_ids": [vid["id"], photo["id"]],
                                                  "root": "NAS", "folder": "atv"}).json()
    assert res["skipped"] == ["p.jpg"] and len(res["jobs"]) == 1
    client.app_state.jobs.wait_idle(240)
    f = next((out / "atv").glob("*.mp4"))
    assert f.name.endswith("DJI_0200 ATV.mp4")              # no GPS → {place} dropped cleanly
    s = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(f)],
                                  capture_output=True, text=True).stdout)["streams"][0]
    assert (s["width"], s["height"], s["codec_name"], s["pix_fmt"]) == (3840, 2160, "hevc", "yuv420p10le")
    assert s["codec_tag_string"] == "hvc1"


def test_place_from_dji_srt_and_cache(tmp_path, monkeypatch):
    clip = tmp_path / "DJI_0300.MP4"
    clip.write_bytes(b"")
    (tmp_path / "DJI_0300.SRT").write_text(
        "1\n00:00:00,000 --> 00:00:00,033\n<font size=\"28\">FrameCnt: 1 [iso: 100] [latitude: -40.501234] "
        "[longitude: 172.671234] [rel_alt: 80.000 abs_alt: 95.0]</font>\n")
    assert geo.gps_for_video(clip, {}) == (-40.501234, 172.671234)
    assert geo.gps_from_iso6709("-40.5012+172.6712+095.000/") == (-40.5012, 172.6712)
    calls = []
    monkeypatch.setattr(geo, "_lookup", lambda lat, lon: calls.append(1) or "Golden Bay")
    geo.configure(tmp_path / "geo.json")
    assert geo.place_name((-40.5012, 172.6712)) == "Golden Bay"
    assert geo.place_name((-40.5013, 172.6711)) == "Golden Bay" and len(calls) == 1   # cached
    monkeypatch.setenv("SIRIS_GEOCODER", "off")
    assert geo.place_name((10.0, 10.0)) == ""


def test_name_tidy_when_place_missing():
    from app.jobs import output_stem
    assert output_stem("{date} {place} {name} IG", "DJI_1", "2026-10-04", 1) == "2026-10-04 DJI_1 IG"
    assert output_stem("{place}_{name}", "DJI_1", "d", 1) == "DJI_1"


def test_top_down_shots_are_never_levelled(client, tmp_path):
    """DJI records the gimbal angle in the photo's XMP; straight-down shots skip auto-level."""
    p = horizon_scene(tmp_path / "DJI_0900.JPG", 5.0)
    data = p.read_bytes()
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           b'<rdf:Description xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" '
           b'drone-dji:GimbalPitchDegree="-90.0"/></rdf:RDF></x:xmpmeta>')
    seg = b"\xff\xe1" + (len(xmp) + 31).to_bytes(2, "big") + b"http://ns.adobe.com/xap/1.0/\x00" + xmp
    p.write_bytes(data[:2] + seg + data[2:])
    assert geo.gimbal_pitch(p) == -90.0
    item = upload(client, p)
    r = client.post(f"/api/media/{item['id']}/auto", json={"exposure": False}).json()
    assert r["top_down"] and not r["horizon_found"]
    srt = tmp_path / "DJI_0901.SRT"
    srt.write_text("[gb_yaw: 12.3 gb_pitch: -88.5 gb_roll: 0.0]")
    assert geo.gimbal_pitch(tmp_path / "DJI_0901.MP4") == -88.5
