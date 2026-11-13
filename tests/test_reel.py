import subprocess

import numpy as np
from PIL import Image

from app import reel, tripmatch, video
from app.edits import normalize_edit
from tests.conftest import make_video
from tests.scenes import scene


def shaky_video(path, seconds=6):
    """A test pattern with the camera jolting about (a 640×360 window wobbling inside it)."""
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=800x450:rate=30", "-t", str(seconds),
                    "-vf", "crop=640:360:'80+60*sin(t*13)':'45+40*cos(t*17)'",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_scores_prefer_steady_footage(tmp_path):
    steady = make_video(tmp_path / "steady.mp4", size="640x360", seconds=6, audio=False)
    shaky = shaky_video(tmp_path / "shaky.mp4")
    scored = []
    for p in (steady, shaky):
        meta = video.probe(p)
        scored.append(reel.score_clip(p, meta, normalize_edit({})))
    assert len(scored[0]["times"]) == 12                       # 2 samples a second
    assert scored[0]["lo"] == 1.0 and scored[0]["hi"] == 5.0    # first/last second skipped
    reel.finish_scores(scored)
    assert scored[1]["penalty"].mean() > scored[0]["penalty"].mean() * 1.5
    assert scored[0]["scores"].mean() > scored[1]["scores"].mean()


def test_shift_finds_translation():
    a = np.asarray(scene(1, 192, 108).convert("L"), np.float32)
    b = np.roll(a, (3, -5), axis=(0, 1))
    assert reel.shift(a, b) == (3.0, -5.0)


def test_plan_takes_each_clips_best_window_in_shooting_order():
    t = np.arange(0, 20, 0.5)
    good = np.where((t >= 10) & (t < 14), 1.0, 0.2)           # clip 0 is best from 10–14 s
    flat = np.full_like(t, 0.5)
    clips = [{"times": t, "scores": good, "lo": 1, "hi": 19, "speed": 1.0},
             {"times": t, "scores": flat, "lo": 1, "hi": 19, "speed": 1.0}]
    picks = reel.plan(clips, length=30, seg=4)
    assert {"clip": 0, "start": 10.0, "end": 14.0, "score": 1.0} in picks
    assert {p["clip"] for p in picks} == {0, 1}
    slots = int((30 - reel.TITLE_SECONDS) // (4 - reel.TRANSITION))
    assert len(picks) == slots
    for a, b in zip(picks, picks[1:]):                          # in order, never overlapping
        assert (a["clip"], a["start"]) < (b["clip"], b["start"])
        if a["clip"] == b["clip"]:
            assert a["end"] <= b["start"]


def test_title_text():
    assert reel.title_text("", "Cowes, Australia") == ("COWES", "AUSTRALIA")
    assert reel.title_text("Wharariki Beach", "x, y") == ("WHARARIKI BEACH", "")
    assert reel.nice_date("2026-04-03") == "3 APRIL 2026"
    assert reel.fps_arg(29.97) == "30000/1001" and reel.fps_arg(25.0) == "25"


def test_reel_api_makes_a_titled_video(client, env, tmp_path):
    from tests.test_api import upload
    _, out, _ = env
    ids = [upload(client, make_video(tmp_path / f"DJI_00{i}.MP4", size="640x360", seconds=6, audio=False))["id"]
           for i in (1, 2)]
    r = client.post("/api/reel", json={"media_ids": ids, "length": 12, "segment": 3, "recipe": "full",
                                       "title": "Test Flight", "root": "NAS", "folder": "Reels"})
    assert r.status_code == 200, r.text
    client.app_state.jobs.wait_idle(300)
    job = client.get("/api/jobs").json()[-1]
    assert job["kind"] == "reel" and job["status"] == "done", job
    meta = video.probe(out / "Reels" / job["output"].split("/")[-1])
    assert "Test Flight highlights" in job["output"]
    assert (meta["width"], meta["height"]) == (640, 360) and not meta["has_audio"]
    # title (3 s) + one 3 s window from each clip (a 6 s clip less its first and last second
    # has room for only one), joined by 0.75 s crossfades
    assert abs(meta["duration"] - (3 + 2 * 3 - 2 * 0.75)) < 0.2
    assert client.post("/api/reel", json={"media_ids": []}).status_code == 400


def _variant(base, gain, cast):
    return Image.fromarray((np.clip(base * gain * np.array(cast), 0, 1) * 255).astype(np.uint8))


def test_trip_match_evens_out_exposure_and_white_balance(client, tmp_path):
    from tests.test_api import upload
    base = np.asarray(scene(3), np.float32) / 255
    shots = [(1, (1, 1, 1)), (0.75, (1, 1, 1)), (1, (1.06, 1, 0.94)), (1.2, (0.96, 1, 1.04)), (1, (1, 1, 1))]
    ids = []
    for k, (gain, cast) in enumerate(shots):
        p = tmp_path / f"DJI_01{k}.JPG"
        _variant(base, gain, cast).save(p, quality=95)
        ids.append(upload(client, p)["id"])
    lib = client.app_state.lib

    def spread():
        st = []
        for mid in ids:
            item = lib.get(mid)
            st.append(tripmatch.measure(lib.proxy(item), lib.get_edit(mid)))
        return {k: float(np.ptp([s[k] for s in st])) for k in ("ev", "rb")}

    before = spread()
    r = client.post("/api/match", json={"media_ids": ids}).json()
    after = spread()
    assert set(r["changed"]) == set(ids[1:4])                   # the odd ones out moved
    assert r["changed"][ids[1]]["exposure"] > 0                 # dark shot brightened
    assert r["changed"][ids[2]]["temperature"] < 0              # warm shot cooled
    assert after["ev"] < before["ev"] * 0.6 and after["rb"] < before["rb"] * 0.6
    assert all(abs(d["exposure"]) <= 0.6 and abs(d["temperature"]) <= 25 for d in r["changed"].values())
    assert client.post("/api/match", json={"media_ids": ids[:1]}).status_code == 400


def test_watch_folder_makes_a_trip_reel_and_keeps_clips_until_it_is_done(client, env):
    _, out, raw = env
    trip = raw / "Inbox" / "2026-04-03_Cowes"
    trip.mkdir(parents=True)
    w = client.app_state.watcher
    w.min_age = 0
    r = client.put("/api/watch", json={"enabled": True, "root": "Footage", "folder": "Inbox", "recursive": True,
                                       "out_root": "NAS", "out_folder": "Auto", "interval": 3600,
                                       "videos": "full", "reel": True, "reel_length": 15,
                                       "delete_originals": True, "auto_level": False, "auto_exposure": False})
    assert r.json()["settings"]["reel"] is True
    for i in (1, 2):
        make_video(trip / f"DJI_00{i}.MP4", size="640x360", seconds=6, audio=False)
    w.scan()
    assert w.scan() == 2                        # clips exported; the reel waits for a quiet scan
    client.app_state.jobs.wait_idle(300)
    w.scan()                                    # exports done, but the originals wait for the reel...
    assert sorted(p.name for p in trip.iterdir()) == ["DJI_001.MP4", "DJI_002.MP4"]
    reels = [j for j in client.app_state.jobs.list() if j["kind"] == "reel"]
    assert len(reels) == 1                      # ...which this quiet scan queued
    client.app_state.jobs.wait_idle(300)
    folder = out / "Auto" / "2026-04-03_Cowes"
    assert len(list(folder.glob("*highlights.mp4"))) == 1, list(folder.iterdir())
    assert len(list(folder.glob("DJI_00*_BRM.mp4"))) == 2
    w.scan()                                    # reel done: now the originals go
    assert list(trip.iterdir()) == []
    assert len([j for j in client.app_state.jobs.list() if j["kind"] == "reel"]) == 1  # made once
