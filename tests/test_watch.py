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
    horizon_scene(inbox / "PANO_0501.JPG", 0, 4000, 1000)        # panorama
    make_video(inbox / "DJI_0502.MP4", size="320x180", seconds=1)
    (inbox / "notes.txt").write_text("ignored")

    assert w.scan() == 0          # first sighting: wait until the copy has finished
    assert w.scan() == 3
    assert w.scan() == 0          # nothing processed twice
    client.app_state.jobs.wait_idle(300)
    jobs = client.get("/api/jobs").json()
    assert sorted(j["kind"] for j in jobs) == ["carousel", "photo", "video"]
    assert all(j["status"] == "done" for j in jobs), jobs

    auto = out / "Auto"
    photo = next(auto.glob("*DJI_0500 IG.jpg"))
    assert Image.open(photo).size == (1080, 1350)
    assert len(list(auto.glob("*PANO_0501 IG_*.jpg"))) == 5
    vid = next(auto.glob("*DJI_0502 ATV.mp4"))
    s = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(vid)],
                                  capture_output=True, text=True).stdout)["streams"][0]
    assert (s["width"], s["height"], s["codec_name"]) == (3840, 2160, "hevc")

    # the photo was levelled and is in the library for later tweaking; nothing in the inbox changed
    item = next(i for i in client.get("/api/media").json() if i["name"] == "DJI_0500.JPG")
    edit = client.get(f"/api/media/{item['id']}/edit").json()
    assert abs(edit["straighten"] + 3.0) <= 0.2 and edit["aspect"] == "4:5"
    assert edit["grade"]["blue_sat"] > 0 and edit["stamp"]["enabled"]
    assert sorted(p.name for p in inbox.iterdir()) == ["DJI_0500.JPG", "DJI_0502.MP4", "OLD_0001.JPG",
                                                       "PANO_0501.JPG", "notes.txt"]
    status = client.get("/api/watch").json()["status"]
    assert status["processed"] == 3 and not status["errors"]
