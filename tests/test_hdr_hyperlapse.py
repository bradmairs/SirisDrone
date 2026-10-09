import json
import subprocess

import numpy as np
from PIL import Image

from tests.test_api import upload


def scene(rng, h=600, w=800):
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    tex = np.asarray(Image.fromarray((rng.random((75, 100)) * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC),
                     np.float32) / 255
    rad = np.where(y < 250, 1.6 + 0.4 * tex, 0.08 + 0.12 * tex)
    return np.stack([rad * 0.8, rad * 0.9, rad], -1)


def shot(rad, ev, dx=0, dy=0):
    img = np.clip((rad * 2 ** ev) ** (1 / 2.2), 0, 1)
    return Image.fromarray((np.roll(img, (dy, dx), (0, 1)) * 255).astype(np.uint8))


def test_hdr_merge_adds_16bit_item(client, tmp_path):
    rad = scene(np.random.default_rng(0))
    ids = []
    for i, (ev, dx, dy) in enumerate([(-1.4, 3, -2), (0, 0, 0), (1.4, -4, 5)]):
        p = tmp_path / f"DJI_00{i}.JPG"
        exif = Image.Exif()
        exif[271] = "DJI"
        shot(rad, ev, dx, dy).save(p, quality=95, exif=exif)
        ids.append(upload(client, p)["id"])
    r = client.post("/api/hdr-merge", json={"media_ids": ids})
    assert r.status_code == 200, r.text
    client.app_state.jobs.wait_idle()
    job = client.get("/api/jobs").json()[-1]
    assert job["status"] == "done", job
    merged = next(i for i in client.get("/api/media").json() if i["name"].endswith("_HDR.tif"))
    assert merged["kind"] == "photo" and merged["width"] > 780
    # editable and previewable like any photo, and keeps the camera EXIF
    assert client.post(f"/api/media/{merged['id']}/preview", json={"size": 400}).status_code == 200
    from app import render
    src, info = render.load_photo(client.app_state.lib.source_path(merged))
    rgb = render.to_float(src)
    assert info["exif"] and Image.Exif().load(info["exif"]) is None
    assert (rgb[20:230].max(-1) > 0.99).mean() < 0.05       # sky no longer clipped
    assert client.post("/api/hdr-merge", json={"media_ids": ids[:1]}).status_code == 400


def test_hyperlapse_from_photos(client, env, tmp_path):
    _, out, _ = env
    rng = np.random.default_rng(1)
    base = (np.clip(scene(rng, 450, 800) ** (1 / 2.2), 0, 1) * 255).astype(np.uint8)
    ids = []
    for i in range(10):
        p = tmp_path / f"HYPERLAPSE_{i:04d}.JPG"
        frame = np.roll(base, (int(rng.integers(-4, 5)), i * 6 + int(rng.integers(-4, 5))), (0, 1))
        Image.fromarray(np.clip(frame * (1 + 0.06 * (i % 2)), 0, 255).astype(np.uint8)).save(p, quality=92)
        ids.append(upload(client, p)["id"])
    r = client.post("/api/hyperlapse", json={"media_ids": ids[::-1], "fps": 10, "root": "NAS", "folder": "hl"})
    assert r.status_code == 200, r.text
    client.app_state.jobs.wait_idle(300)
    job = client.get("/api/jobs").json()[-1]
    assert job["status"] == "done", job
    f = next((out / "hl").glob("*.mp4"))
    assert "HYPERLAPSE_0000 hyperlapse" in f.name               # ordered by name, not selection
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                      "-show_format", str(f)], capture_output=True, text=True).stdout)
    v = info["streams"][0]
    assert v["codec_name"] == "hevc" and (v["width"], v["height"]) == (3840, 2160)
    assert abs(float(info["format"]["duration"]) - 1.0) < 0.15
