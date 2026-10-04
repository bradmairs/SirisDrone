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


def test_new_items_start_with_brm_blue():
    from app.edits import normalize_edit
    e = normalize_edit({})
    assert e["grade"] == G.normalize_grade(G.PRESETS["BRM Blue"])
    assert e["grade"]["blue_sat"] > 0
    # an explicit grade is taken as-is (missing keys are neutral)
    assert normalize_edit({"grade": {}})["grade"] == G.DEFAULT_GRADE


def test_blues_controls_only_touch_blues():
    px = np.array([[[0.25, 0.50, 0.85],    # sky blue
                    [0.10, 0.35, 0.55],    # deep water
                    [0.30, 0.55, 0.20],    # grass
                    [0.85, 0.80, 0.70],    # sand
                    [0.70, 0.30, 0.20],    # red cliff
                    [0.50, 0.50, 0.50]]],  # grey
                  np.float32)
    out = G.grade_pixels(px, G.normalize_grade({"blue_sat": 80, "blue_lum": -40}))[0]
    chroma = lambda c: c.max() - c.min()
    assert chroma(out[0]) > chroma(px[0, 0]) + 0.05 and out[0] @ G.LUMA < px[0, 0] @ G.LUMA
    assert chroma(out[1]) > chroma(px[0, 1])
    assert np.allclose(out[2:], px[0, 2:], atol=0.01)


def test_brm_blue_makes_skies_bluer_without_shifting_greens():
    sky = np.array([[[0.45, 0.60, 0.75]]], np.float32)
    grass = np.array([[[0.30, 0.45, 0.20]]], np.float32)
    g = G.normalize_grade(G.HOUSE_GRADE)
    s2, g2 = G.grade_pixels(sky, g)[0, 0], G.grade_pixels(grass, g)[0, 0]
    assert (s2[2] - s2[0]) > (0.75 - 0.45) + 0.1   # sky noticeably more blue
    assert g2[1] == g2.max()                        # grass is still green
