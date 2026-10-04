import subprocess
import json

from PIL import Image

from tests.conftest import make_photo, make_video


def upload(client, path):
    with open(path, "rb") as fh:
        r = client.post("/api/media/upload", files=[("files", (path.name, fh, "application/octet-stream"))])
    assert r.status_code == 200, r.text
    assert not r.json()["errors"]
    return r.json()["added"][0]


def test_photo_upload_edit_preview_export(client, env, tmp_path):
    _, out, _ = env
    item = upload(client, make_photo(tmp_path / "DJI_0100.JPG"))
    assert item["kind"] == "photo" and (item["width"], item["height"]) == (1600, 1200)
    assert client.get(f"/api/media/{item['id']}/thumb").status_code == 200

    edit = client.get(f"/api/media/{item['id']}/edit").json()
    assert edit["stamp"]["enabled"] and edit["stamp"]["position"] == "br"
    edit["crop"] = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}
    edit["grade"]["contrast"] = 25
    client.put(f"/api/media/{item['id']}/edit", json=edit)

    r = client.post(f"/api/media/{item['id']}/preview", json={"size": 600})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert (int(r.headers["x-width"]), int(r.headers["x-height"])) == (600, 450)
    r = client.post(f"/api/media/{item['id']}/preview", json={"size": 600, "mode": "frame"})
    assert int(r.headers["x-width"]) == 600

    r = client.post("/api/export", json={"media_ids": [item["id"]],
                                         "options": {"root": "NAS", "folder": "2026/Beach", "strip_gps": True}})
    assert r.status_code == 200, r.text
    client.app_state.jobs.wait_idle()
    job = client.get("/api/jobs").json()[0]
    assert job["status"] == "done", job
    f = out / "2026" / "Beach" / "DJI_0100_BRM.jpg"
    assert f.exists()
    im = Image.open(f)
    assert im.size == (800, 600)  # full resolution of the crop
    assert 0x8825 not in im.getexif()
    assert not list((out / "2026" / "Beach").glob(".*.part"))

    # second export does not overwrite
    client.post("/api/export", json={"media_ids": [item["id"]], "options": {"root": "NAS", "folder": "2026/Beach"}})
    client.app_state.jobs.wait_idle()
    assert (out / "2026" / "Beach" / "DJI_0100_BRM-2.jpg").exists()
    # export settings are remembered
    assert client.get("/api/config").json()["settings"]["export"]["folder"] == "2026/Beach"


def test_video_import_trim_export(client, env, tmp_path):
    _, out, raw = env
    make_video(raw / "DJI_0200.MP4", seconds=3)
    r = client.post("/api/media/import", json={"root": "Footage", "path": ""})
    item = r.json()["added"][0]
    assert item["kind"] == "video" and item["source"] == "linked" and item["has_audio"]

    edit = client.get(f"/api/media/{item['id']}/edit").json()
    edit["video"] = {"trim_start": 0.5, "trim_end": 2.0, "speed": 1, "mute": False}
    edit["crop"] = {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}
    edit["grade"]["saturation"] = 30
    client.put(f"/api/media/{item['id']}/edit", json=edit)
    assert client.post(f"/api/media/{item['id']}/preview", json={"t": 1.0, "size": 400}).status_code == 200

    client.post("/api/export", json={"media_ids": [item["id"]],
                                     "options": {"root": "NAS", "video_size": "1280", "video_codec": "h264"}})
    client.post("/api/export", json={"media_ids": [item["id"]], "options": {"root": "NAS"}, "frame_time": 1.0})
    client.app_state.jobs.wait_idle(180)
    jobs = client.get("/api/jobs").json()
    assert [j["status"] for j in jobs] == ["done", "done"], jobs

    probe = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                            str(out / "DJI_0200_BRM.mp4")], capture_output=True, text=True, check=True)
    info = json.loads(probe.stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1280, 720)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - 1.5) < 0.15
    assert Image.open(out / "DJI_0200_00m01-00s_BRM.jpg").size == (320, 180)


def test_path_traversal_is_refused(client):
    assert client.get("/api/browse", params={"kind": "output", "root": "NAS", "path": "../../"}).status_code == 400
    assert client.post("/api/mkdir", json={"root": "NAS", "path": "../escape"}).status_code == 400
    r = client.post("/api/export", json={"media_ids": [], "options": {"root": "NAS", "folder": "../../etc"}})
    assert r.status_code == 400
    assert client.post("/api/media/import", json={"root": "Footage", "path": "../../../etc/passwd"}).status_code == 400
    assert client.post("/api/media/import", json={"root": "Footage", "path": "/etc/passwd"}).status_code == 404
    r = client.post("/api/media/import", json={"root": "Footage", "path": "", "files": ["../../secret.jpg"]})
    assert r.json()["added"] == []


def test_rejects_unsupported_upload(client, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    with open(p, "rb") as fh:
        r = client.post("/api/media/upload", files=[("files", ("notes.txt", fh, "text/plain"))])
    assert r.json()["added"] == [] and r.json()["errors"]


def test_lut_upload_and_use(client, tmp_path):
    from app import grading
    p = tmp_path / "look.cube"
    grading.write_cube(p, 1 - grading.identity_grid(5))
    with open(p, "rb") as fh:
        lut = client.post("/api/luts", files=[("file", ("My Look.cube", fh, "text/plain"))]).json()
    assert client.get("/api/luts").json() == [{"id": lut["id"], "name": "My Look"}]
    item = upload(client, make_photo(tmp_path / "x.jpg", 300, 200))
    edit = client.get(f"/api/media/{item['id']}/edit").json()
    edit["grade"]["lut"] = lut["id"]
    edit["stamp"]["enabled"] = False
    r = client.post(f"/api/media/{item['id']}/preview", json={"edit": edit, "size": 300})
    assert r.status_code == 200
    assert client.delete(f"/api/luts/{lut['id']}").status_code == 200


def test_unmounted_nas_is_not_created(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Config
    from app.main import create_app
    missing = tmp_path / "mnt" / "nas"
    app = create_app(Config({"SIRIS_DATA_DIR": str(tmp_path / "d"), "SIRIS_OUTPUT_ROOTS": f"NAS={missing}"}))
    assert not missing.exists()
    with TestClient(app) as c:
        assert c.get("/api/config").json()["output_roots"][0]["available"] is False
        r = c.post("/api/export", json={"media_ids": [], "options": {"root": "NAS"}})
        assert r.status_code == 400 and "not reachable" in r.json()["detail"]
    assert not missing.exists()


def test_export_name_pattern_and_custom_size(client, env, tmp_path):
    _, out, _ = env
    a = upload(client, make_photo(tmp_path / "DJI_0001.JPG", 1600, 1200))
    b = upload(client, make_photo(tmp_path / "DJI_0002.JPG", 1600, 1200))
    opts = {"root": "NAS", "folder": "named", "name_pattern": "Golden Bay {n} {name}", "photo_size": "1080x1350"}
    r = client.post("/api/export", json={"media_ids": [a["id"], b["id"]], "options": opts})
    assert r.status_code == 200, r.text
    client.app_state.jobs.wait_idle()
    # newest upload first in the request order given
    assert Image.open(out / "named" / "Golden Bay 001 DJI_0001.jpg").size == (1080, 810)  # fits the box, keeps 4:3
    assert (out / "named" / "Golden Bay 002 DJI_0002.jpg").exists()
    # height-only box and a custom long edge
    client.post("/api/export", json={"media_ids": [a["id"]], "options": {"root": "NAS", "folder": "h",
                                                                          "photo_size": "x600", "name_pattern": "{name}"}})
    client.post("/api/export", json={"media_ids": [a["id"]], "options": {"root": "NAS", "folder": "le",
                                                                          "photo_size": "1000", "name_pattern": "../{name}"}})
    client.app_state.jobs.wait_idle()
    assert Image.open(out / "h" / "DJI_0001.jpg").size == (800, 600)
    assert Image.open(out / "le" / "DJI_0001.jpg").size == (1000, 750)  # "../" stripped, stays in folder
    saved = client.get("/api/config").json()["settings"]["export"]
    assert saved["photo_size"] == "1000" or saved["photo_size"] == 1000


def test_old_suffix_setting_still_works():
    from app.jobs import normalize_export
    assert normalize_export({"suffix": "_web"})["name_pattern"] == "{name}_web"
    assert normalize_export({})["name_pattern"] == "{name}_BRM"


def test_video_playback_original_and_edited(client, env, tmp_path):
    _, out, raw = env
    make_video(raw / "h264.mp4", seconds=2)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=30", "-t", "2", "-c:v", "libx265", "-x265-params", "log-level=error",
                    "-pix_fmt", "yuv420p10le", str(raw / "hevc.mp4")], check=True)
    items = {i["name"]: i for i in client.post("/api/media/import", json={"root": "Footage", "path": ""}).json()["added"]}
    pb = client.app_state.playback

    # H.264 8-bit plays as-is, with byte-range support for seeking
    st = client.post(f"/api/media/{items['h264.mp4']['id']}/playback", json={"mode": "original"}).json()
    assert st["status"] == "ready" and "url" in st and "path" not in st
    r = client.get(st["url"], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100

    # HEVC 10-bit gets an H.264 copy
    hid = items["hevc.mp4"]["id"]
    st = client.post(f"/api/media/{hid}/playback", json={"mode": "original"}).json()
    assert st["status"] in ("queued", "rendering", "ready")
    st = pb.wait(st["key"])
    assert st["status"] == "ready", st
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", st["path"]],
                                     capture_output=True, text=True).stdout)["streams"][0]
    assert info["codec_name"] == "h264" and info["pix_fmt"] == "yuv420p"

    # edited preview: trim + speed + crop applied, cached by edit
    edit = client.get(f"/api/media/{hid}/edit").json()
    edit["video"] = {"trim_start": 0.5, "trim_end": 1.5, "speed": 2, "mute": True}
    edit["crop"] = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}
    st = client.post(f"/api/media/{hid}/playback", json={"mode": "edited", "edit": edit}).json()
    st = pb.wait(st["key"])
    assert st["status"] == "ready", st
    fmt = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                                     st["path"]], capture_output=True, text=True).stdout)
    assert abs(float(fmt["format"]["duration"]) - 0.5) < 0.15
    assert (fmt["streams"][0]["width"], fmt["streams"][0]["height"]) == (320, 180)
    again = client.post(f"/api/media/{hid}/playback", json={"mode": "edited", "edit": edit}).json()
    assert again["status"] == "ready" and again["key"] == st["key"]
    assert client.get(again["url"]).status_code == 200

    # photos can't be played
    p = upload(client, make_photo(tmp_path / "p.jpg", 300, 200))
    assert client.post(f"/api/media/{p['id']}/playback", json={"mode": "original"}).status_code == 400
