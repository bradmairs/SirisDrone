import json
import subprocess

from PIL import Image

from tests.conftest import make_video
from tests.test_auto import horizon_scene


def test_watch_folder_auto_edits_and_exports(client, env, tmp_path):
    _, out, raw = env
    inbox = raw / "Inbox"
    inbox.mkdir()
    horizon_scene(inbox / "OLD_0001.JPG", 0, 800, 600)          # already there: left alone
    w = client.app_state.watcher
    w.min_age = 0

    bad = client.put("/api/watch", json={"enabled": True, "root": "Nope"})
    assert bad.status_code == 400
    r = client.put("/api/watch", json={"enabled": True, "root": "Footage", "folder": "Inbox",
                                       "out_root": "NAS", "out_folder": "Auto", "interval": 3600})
    assert r.status_code == 200, r.text
    assert r.json()["skipped_existing"] == 1

    horizon_scene(inbox / "DJI_0500.JPG", 3.0, 1600, 1200)       # tilted photo
    make_video(inbox / "DJI_0502.MP4", size="320x180", seconds=1)
    (inbox / "notes.txt").write_text("ignored")

    assert w.scan() == 0          # first sighting: wait until the copy has finished
    assert w.scan() == 2
    assert w.scan() == 0          # nothing processed twice
    client.app_state.jobs.wait_idle(300)
    jobs = client.get("/api/jobs").json()
    assert sorted(j["kind"] for j in jobs) == ["photo", "video"]
    assert all(j["status"] == "done" for j in jobs), jobs

    auto = out / "Auto"
    w_, h_ = Image.open(auto / "DJI_0500_BRM.jpg").size  # full resolution (levelling zooms in a little)
    assert w_ > 1400 and abs(w_ / h_ - 4 / 3) < 0.01
    vid = next(auto.glob("*DJI_0502 ATV.mp4"))
    s = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(vid)],
                                  capture_output=True, text=True).stdout)["streams"][0]
    assert (s["width"], s["height"], s["codec_name"]) == (3840, 2160, "hevc")

    # the photo was levelled and is in the library for later tweaking; nothing in the inbox changed
    item = next(i for i in client.get("/api/media").json() if i["name"] == "DJI_0500.JPG")
    edit = client.get(f"/api/media/{item['id']}/edit").json()
    assert abs(edit["straighten"] + 3.0) <= 0.2 and edit["aspect"] == "original"
    assert edit["grade"]["blue_sat"] > 0 and edit["stamp"]["enabled"]
    assert sorted(p.name for p in inbox.iterdir()) == ["DJI_0500.JPG", "DJI_0502.MP4", "OLD_0001.JPG",
                                                       "notes.txt"]
    status = client.get("/api/watch").json()["status"]
    assert status["processed"] == 2 and not status["errors"]


def test_watch_folder_deletes_originals_once_exported(client, env):
    _, out, raw = env
    inbox = raw / "To Edit"
    inbox.mkdir()
    old = raw / "Originals"
    old.mkdir()
    w = client.app_state.watcher
    w.min_age = 0
    horizon_scene(inbox / "OLD_0001.JPG", 0, 800, 600)          # there before: never deleted
    r = client.put("/api/watch", json={"enabled": True, "root": "Footage", "folder": "To Edit",
                                       "out_root": "NAS", "out_folder": "Auto", "interval": 3600,
                                       "delete_originals": True, "photos": "full", "videos": "skip"})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["delete_originals"] is True

    horizon_scene(inbox / "DJI_0600.JPG", 0, 800, 600)
    make_video(inbox / "DJI_0601.MP4", size="320x180", seconds=1)  # "edit only": kept
    assert w.scan() == 0 and w.scan() == 2
    assert (inbox / "DJI_0600.JPG").exists()                       # not until the export is done
    client.app_state.jobs.wait_idle(300)
    w.scan()
    assert next((out / "Auto").glob("*DJI_0600*.jpg")).exists()
    assert sorted(p.name for p in inbox.iterdir()) == ["DJI_0601.MP4", "OLD_0001.JPG"]
    assert "DJI_0600.JPG" not in [i["name"] for i in client.get("/api/media").json()]

    # A failed export keeps the file.
    horizon_scene(inbox / "DJI_0602.JPG", 0, 800, 600)
    w.scan(); w.scan()
    entry = w.seen[str(inbox / "DJI_0602.JPG")]
    client.app_state.jobs.wait_idle(300)
    job = client.app_state.jobs.jobs[entry["job"]]
    job.status, job.error = "error", "disk full"
    w.scan()
    assert (inbox / "DJI_0602.JPG").exists() and "disk full" in w.seen[str(inbox / "DJI_0602.JPG")]["kept"]

    # Files picked up before the setting was on, or outside the watch folder, are left alone.
    keep = old / "DJI_0001.JPG"
    horizon_scene(keep, 0, 800, 600)
    st = keep.stat()
    w.seen[str(keep)] = {"size": st.st_size, "mtime": st.st_mtime, "item": None, "job": entry["job"],
                         "delete_after": True}
    job.status, job.error = "done", None
    w.scan()
    assert keep.exists()


def test_watch_folder_old_instagram_settings_become_full(client):
    w = client.put("/api/watch", json={"photos": "both"}).json()["settings"]
    assert w["photos"] == "full"
    assert client.put("/api/watch", json={"photos": "instagram"}).json()["settings"]["photos"] == "full"


def test_watch_folder_uses_jpeg_and_skips_its_dng_twin(client, env):
    _, out, raw = env
    inbox = raw / "Twins"
    inbox.mkdir()
    w = client.app_state.watcher
    w.min_age = 0
    r = client.put("/api/watch", json={"enabled": True, "root": "Footage", "folder": "Twins",
                                       "out_root": "NAS", "out_folder": "T", "interval": 3600,
                                       "photos": "full", "delete_originals": True})
    assert r.status_code == 200
    horizon_scene(inbox / "DJI_0800.JPG", 0, 800, 600)
    (inbox / "DJI_0800.DNG").write_bytes(b"raw twin (never decoded: it is skipped)")
    w.scan(); w.scan()
    dng, jpg = str(inbox / "DJI_0800.DNG"), str(inbox / "DJI_0800.JPG")
    assert w.seen[dng]["action"] == "skipped: JPEG used" and w.seen[jpg]["action"] == "full"
    client.app_state.jobs.wait_idle(300)
    w.scan()  # JPEG exported and deleted
    w.scan()  # then its DNG twin goes too
    assert not any(inbox.iterdir())
    assert len(list((out / "T").glob("DJI_0800*.jpg"))) == 1
