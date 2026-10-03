import numpy as np
import pytest

from app import grading as G


def test_neutral_grade_is_identity():
    img = np.random.default_rng(1).random((50, 60, 3), dtype=np.float32)
    assert G.is_neutral({})
    assert np.array_equal(G.grade_image(img, {}), img)
    lut = G.build_lut({}, 17)
    assert np.allclose(G.apply_lut(img, lut), img, atol=1e-5)


def test_lut_matches_direct_grade():
    img = np.random.default_rng(2).random((200, 300, 3), dtype=np.float32)
    g = {"contrast": 30, "temperature": 25, "vibrance": 40, "shadows": 20, "highlights": -30,
         "dehaze": 25, "shadow_sat": 30, "fade": 10}
    direct = G.grade_pixels(img, G.normalize_grade(g))
    via_lut = G.apply_lut(img, G.build_lut(g, 65))
    assert np.abs(direct - via_lut).mean() < 2e-3


@pytest.mark.parametrize("key", ["exposure", "contrast", "shadows", "highlights", "whites",
                                 "blacks", "dehaze", "saturation", "vibrance", "temperature"])
def test_tone_curves_stay_monotonic(key):
    ramp = np.linspace(0, 1, 256, dtype=np.float32)
    grey = np.stack([ramp] * 3, -1)
    for v in (-100, 100) if key != "exposure" else (-3, 3):
        out = G.grade_pixels(grey, G.normalize_grade({key: v}))
        y = out @ G.LUMA
        assert np.all(np.diff(y) >= -1e-4), f"{key}={v} is not monotonic"


def test_warmer_temperature_adds_red():
    grey = np.full((1, 1, 3), 0.5, np.float32)
    out = G.grade_pixels(grey, G.normalize_grade({"temperature": 50}))[0, 0]
    assert out[0] > out[2]


def test_cube_round_trip(tmp_path):
    lut = G.build_lut({"contrast": 40, "saturation": -50}, 17)
    p = tmp_path / "x.cube"
    G.write_cube(p, lut)
    back = G.read_cube(p)
    assert back.shape == lut.shape
    assert np.allclose(back, lut, atol=1e-5)


def test_user_lut_is_applied(tmp_path):
    inv = 1.0 - G.identity_grid(9)  # a negative LUT
    p = tmp_path / "neg.cube"
    G.write_cube(p, inv)
    G.set_lut_loader(lambda lid: p if lid == "neg" else None)
    try:
        px = np.array([[[0.2, 0.4, 0.9]]], np.float32)
        out = G.grade_image(px, {"lut": "neg", "lut_strength": 100})
        assert np.allclose(out, 1 - px, atol=1e-3)
        half = G.grade_image(px, {"lut": "neg", "lut_strength": 50})
        assert np.allclose(half, 0.5, atol=1e-3)
    finally:
        G.set_lut_loader(None)


def test_bad_cube_rejected(tmp_path):
    p = tmp_path / "bad.cube"
    p.write_text("LUT_1D_SIZE 4\n0 0 0\n")
    with pytest.raises(ValueError):
        G.read_cube(p)
