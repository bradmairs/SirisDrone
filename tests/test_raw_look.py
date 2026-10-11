import numpy as np
from PIL import Image

from app import analysis, render


def test_fit_tone_curve_recovers_the_camera_curve():
    rng = np.random.default_rng(0)
    raw = rng.uniform(0, 0.8, (200, 300, 1)).repeat(3, axis=2).astype(np.float32)
    jpeg = np.clip(1 - (1 - raw) ** 2.2, 0, 1)                  # brighter, contrasty "camera JPEG"
    c = render.fit_tone_curve(raw, jpeg)
    xs = np.linspace(0, 1, len(c))
    probe = np.array([0.1, 0.25, 0.5])
    assert np.allclose(np.interp(probe, xs, c), 1 - (1 - probe) ** 2.2, atol=0.05)
    assert np.all(np.diff(c) >= 0) and c[0] == 0 and c[-1] == 1


def test_sibling_jpeg_next_to_dng_or_in_jpeg_folder(tmp_path):
    (tmp_path / "DJI_0001.DNG").write_bytes(b"x")
    (tmp_path / "DJI_0001.JPG").write_bytes(b"x")
    assert render.sibling_jpeg(tmp_path / "DJI_0001.DNG") == tmp_path / "DJI_0001.JPG"
    (tmp_path / "DNG").mkdir()
    (tmp_path / "JPEG").mkdir()
    (tmp_path / "DNG" / "DJI_0002.DNG").write_bytes(b"x")
    (tmp_path / "JPEG" / "DJI_0002.JPG").write_bytes(b"x")
    assert render.sibling_jpeg(tmp_path / "DNG" / "DJI_0002.DNG") == tmp_path / "JPEG" / "DJI_0002.JPG"
    assert render.sibling_jpeg(tmp_path / "DNG" / "DJI_0003.DNG") is None


def test_dng_without_jpeg_gets_the_dji_curve(tmp_path):
    p = tmp_path / "DJI_0009.DNG"
    p.write_bytes(b"not decoded: no JPEG to match")
    lut, gain = render.raw_look(p)
    assert gain == render.DJI_JPEG_SATURATION
    assert lut.shape == (65536,) and lut[0] == 0 and lut[-1] == 65535
    assert 0.5 < lut[int(0.25 * 65535)] / 65535 < 0.6          # roughly doubles mid-tones


def test_auto_exposure_not_blocked_by_saturated_blue_sky():
    h, w = 300, 400
    img = np.zeros((h, w, 3), np.float32)
    img[: h // 2] = (0.05, 0.12, 0.45)                         # deep blue sky (blue near max after BRM Blue)
    img[h // 2:] = (0.12, 0.13, 0.10)                          # dark land
    src = Image.fromarray((img * 255).astype(np.uint8))
    assert analysis.auto_exposure(src, {}) > 0.3                # used to be held at 0


def test_auto_exposure_leaves_well_exposed_shots():
    rng = np.random.default_rng(2)
    img = np.clip(rng.normal(0.45, 0.18, (300, 400, 1)), 0, 1).repeat(3, axis=2)
    assert analysis.auto_exposure(Image.fromarray((img * 255).astype(np.uint8)), {"grade": {}}) == 0.0
