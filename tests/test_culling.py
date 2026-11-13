import datetime as dt

from PIL import Image, ImageFilter

from app import culling
from tests.scenes import jitter, scene, shaken


def save(img, path, when=None):
    exif = Image.Exif()
    exif[271] = "DJI"
    if when:
        exif.get_ifd(0x8769)[36867] = when.strftime("%Y:%m:%d %H:%M:%S")
    img.save(path, quality=95, exif=exif)
    return path


def test_decide_bursts_blur_and_smooth_scenes():
    shots = {f"DJI_00{10 + i}": scene(i) for i in range(5)}
    burst = scene(42)
    shots.update({"DJI_0020": shaken(burst, 4), "DJI_0021": jitter(burst, 1), "DJI_0022": shaken(jitter(burst, 2))})
    shots["DJI_0030"] = scene(7, smooth=True)                   # calm sea: sharp but little detail
    shots["DJI_0040"] = scene(9).filter(ImageFilter.GaussianBlur(4))
    feats = [(n, n + ".JPG", culling.features(im, None, n + ".JPG")) for n, im in shots.items()]
    res = culling.decide(feats)
    assert res["DJI_0021"]["status"] == "keep"                  # the crisp frame of the burst
    assert res["DJI_0020"]["reason"] == res["DJI_0022"]["reason"] == "duplicate of DJI_0021.JPG"
    assert res["DJI_0030"]["status"] == "keep"                  # smooth is not blurry
    assert res["DJI_0040"] == {"status": "reject", "reason": "blurry", "group": None}
    assert all(res[f"DJI_00{10 + i}"]["status"] == "keep" for i in range(5))


def test_similar_shots_far_apart_in_time_are_not_duplicates(tmp_path):
    a = scene(3)
    t = dt.datetime(2026, 4, 3, 10, 0, 0)
    f1 = culling.features(a, Image.open(save(a, tmp_path / "a.jpg", t)).getexif().tobytes(), "DJI_0100.JPG")
    f2 = culling.features(a, Image.open(save(a, tmp_path / "b.jpg", t + dt.timedelta(minutes=5)))
                          .getexif().tobytes(), "DJI_0101.JPG")
    assert f2["time"] - f1["time"] == 300
    res = culling.decide([("a", "DJI_0100.JPG", f1), ("b", "DJI_0101.JPG", f2)])
    assert res["a"]["status"] == res["b"]["status"] == "keep"   # same view, revisited later: keep both


def test_cull_api_and_override(client, env, tmp_path):
    from tests.test_api import upload
    when = dt.datetime(2026, 4, 3, 10, 0, 0)
    burst = scene(42)
    ids = {}
    for i, img in enumerate([jitter(burst, 1), shaken(burst, 6), scene(1), scene(2)]):
        p = save(img, tmp_path / f"DJI_00{50 + i}.JPG", when + dt.timedelta(seconds=i * 2))
        ids[p.name] = upload(client, p)["id"]
    r = client.post("/api/cull", json={}).json()
    assert r["checked"] == 4
    assert r["rejected"] == {ids["DJI_0051.JPG"]: "duplicate of DJI_0050.JPG"}
    item = next(i for i in client.get("/api/media").json() if i["id"] == ids["DJI_0051.JPG"])
    assert culling.effective(item) == "reject"
    client.put(f"/api/media/{ids['DJI_0051.JPG']}/cull", json={"override": "keep"})
    item = next(i for i in client.get("/api/media").json() if i["id"] == ids["DJI_0051.JPG"])
    assert culling.effective(item) == "keep"
    assert client.put(f"/api/media/{ids['DJI_0051.JPG']}/cull", json={"override": "maybe"}).status_code == 400


def test_watch_folder_skips_rejects_and_keeps_their_originals(client, env, tmp_path):
    _, out, raw = env
    inbox = raw / "Inbox" / "2026-04-03_Wharariki"
    inbox.mkdir(parents=True)
    w = client.app_state.watcher
    w.min_age = 0
    r = client.put("/api/watch", json={"enabled": True, "root": "Footage", "folder": "Inbox", "recursive": True,
                                       "out_root": "NAS", "out_folder": "Auto", "interval": 3600,
                                       "delete_originals": True, "auto_level": False, "auto_exposure": False})
    assert r.status_code == 200, r.text
    when = dt.datetime(2026, 4, 3, 10, 0, 0)
    burst = scene(42)
    save(jitter(burst, 1), inbox / "DJI_0060.JPG", when)
    save(shaken(burst, 6), inbox / "DJI_0061.JPG", when + dt.timedelta(seconds=1))
    save(scene(5), inbox / "DJI_0062.JPG", when + dt.timedelta(seconds=40))
    save(scene(6).filter(ImageFilter.GaussianBlur(5)), inbox / "DJI_0063.JPG", when + dt.timedelta(seconds=80))
    assert w.scan() == 0
    assert w.scan() == 4
    client.app_state.jobs.wait_idle(300)
    exported = sorted(p.name for p in (out / "Auto").rglob("*.jpg"))
    assert exported == ["DJI_0060_BRM.jpg", "DJI_0062_BRM.jpg"]
    w.scan()  # deletes exported originals
    left = sorted(p.name for p in inbox.iterdir())
    assert left == ["DJI_0061.JPG", "DJI_0063.JPG"]             # rejects are never deleted
    actions = {e["name"]: e["action"] for e in client.get("/api/watch").json()["status"]["recent"]}
    assert actions["DJI_0061.JPG"] == "rejected: duplicate of DJI_0060.JPG"
    assert actions["DJI_0063.JPG"] == "rejected: blurry"
