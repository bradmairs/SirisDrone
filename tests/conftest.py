import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.config import Config
from app.main import create_app


def make_photo(path: Path, w=1600, h=1200, gps=True) -> Path:
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([x / w, y / h, 0.5 + 0.4 * np.sin(x / 40) * np.cos(y / 30)], -1)
    img = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    exif = Image.Exif()
    exif[271] = "DJI"
    if gps:
        exif[0x8825] = {1: "S", 2: (33.0, 51.0, 35.0), 3: "E", 4: (151.0, 12.0, 40.0)}
    Image.fromarray(img).save(path, quality=95, exif=exif)
    return path


def make_video(path: Path, size="640x360", seconds=2, audio=True) -> Path:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440"]
    cmd += ["-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709"]
    if audio:
        cmd += ["-c:a", "aac"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


@pytest.fixture(autouse=True)
def no_city_lookups(monkeypatch):
    """Tests never call OpenStreetMap; location tests patch in their own address."""
    from app import geo
    monkeypatch.setattr(geo, "_lookup_address", lambda lat, lon: {})


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "nas" / "exports"
    raw = tmp_path / "nas" / "footage"
    out.mkdir(parents=True)
    raw.mkdir(parents=True)
    cfg = Config({"SIRIS_DATA_DIR": str(tmp_path / "data"),
                  "SIRIS_OUTPUT_ROOTS": f"NAS={out}",
                  "SIRIS_INPUT_ROOTS": f"Footage={raw}"})
    return cfg, out, raw


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    cfg, out, raw = env
    app = create_app(cfg)
    with TestClient(app) as c:
        c.app_state = app.state
        yield c
