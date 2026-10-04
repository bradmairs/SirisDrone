import subprocess

import numpy as np
import pytest
from PIL import Image

from app import render, video
from app.config import DEFAULT_STAMP
from app.edits import normalize_edit, plan_geometry, straighten_scale
from app.stamp import load_mask, render_stamp


def test_straighten_scale_keeps_corners_inside():
    for angle in (1, 5, 15, -10):
        k = straighten_scale(4000, 3000, angle)
        assert 0 < k < 1
    assert straighten_scale(4000, 3000, 0) == 1


def test_geometry_crop_and_rotate():
    e = normalize_edit({"rotate": 90, "crop": {"x": 0.25, "y": 0, "w": 0.5, "h": 1}})
    g = plan_geometry(4000, 3000, e)
    assert (g.ow, g.oh) == (3000, 4000)
    assert g.output_size() == (1500, 4000)
    assert g.output_size(1000) == (375, 1000)


def test_normalize_edit_clamps_garbage():
    e = normalize_edit({"crop": {"x": 0.9, "w": 0.5}, "sharpen": 900, "stamp": {"position": "zz", "color": "red"},
                        "video": {"trim_start": 5, "trim_end": 2, "speed": 99}})
    assert e["crop"]["x"] == pytest.approx(0.5)
    assert e["sharpen"] == 100
    assert e["stamp"]["position"] == "br" and e["stamp"]["color"] == "#000000"
    assert e["video"]["trim_end"] is None and e["video"]["speed"] == 4


def test_default_stamp_matches_brm_posts():
    """Black script, no shadow, ~24% of the short edge wide, ~2.7% in from the bottom-right corner."""
    mask = load_mask(DEFAULT_STAMP)
    s = normalize_edit({})["stamp"]
    assert s["color"] == "#000000" and s["shadow"] == 0 and s["position"] == "br"
    img, x, y = render_stamp(mask, 1320, 992, s)
    px = np.asarray(img)
    ys, xs = np.nonzero(px[..., 3] > 128)
    assert px[..., :3][px[..., 3] > 128].max() < 10           # black ink
    assert abs((xs.max() - xs.min()) / 992 - 0.24) < 0.01      # width like the example post
    assert abs(1320 - (x + xs.max()) - 27) <= 3                # right margin ≈ 26px in the example
    assert abs(992 - (y + ys.max()) - 27) <= 3                 # bottom margin ≈ 28px


def test_render_photo_applies_everything():
    src = Image.fromarray(np.full((600, 800, 3), 128, np.uint8))
    mask = load_mask(DEFAULT_STAMP)
    edit = {"crop": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, "vignette": -80, "grade": {"exposure": 1},
            "stamp": {"enabled": True, "color": "#ffffff"}}
    out = render.render(src, edit, stamp_mask=mask)
    assert out.shape == (300, 400, 3)
    centre = out[150, 200]
    assert centre.mean() > 0.6                         # exposure brightened mid-grey
    assert out[2, 2].mean() < centre.mean() - 0.1      # vignette darkened the corner
    assert out[260:296, 250:396].max() > 0.9           # white stamp bottom-right


def test_photo_matches_ffmpeg_pipeline(tmp_path):
    """The video export graph must produce the same picture as the photo/preview renderer."""
    h, w = 360, 640
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([xx / w, yy / h, 0.5 + 0.5 * np.sin(xx / 37) * np.cos(yy / 23)], -1)
    img = (img * 255).astype(np.uint8)
    src_png = tmp_path / "src.png"
    Image.fromarray(img).save(src_png)
    mask = load_mask(DEFAULT_STAMP)
    edit = {"rotate": 90, "flip_h": True, "straighten": -6, "crop": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
            "grade": {"contrast": 30, "temperature": 20, "vibrance": 40, "shadow_sat": 30},
            "vignette": -50, "sharpen": 30, "stamp": {"enabled": True}}
    photo = render.render(Image.open(src_png).convert("RGB"), edit, stamp_mask=mask)
    meta = {"width": w, "height": h, "duration": 1, "color_space": None}
    g = video.build_graph(meta, edit, tmp_path, mask, out_pix_fmt="yuv444p10le")
    filt = g["filter"].replace("[vout]", "[v1]") + ";[v1]scale=in_color_matrix=bt709:in_range=tv,format=rgb24[vout]"
    out_png = tmp_path / "ff.png"
    subprocess.run([video.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src_png), *g["inputs"],
                    "-filter_complex", filt, "-map", "[vout]", "-frames:v", "1", str(out_png)], check=True)
    ff = np.asarray(Image.open(out_png).convert("RGB"), np.float32) / 255
    hh, ww = min(photo.shape[0], ff.shape[0]), min(photo.shape[1], ff.shape[1])
    mse = np.mean((photo[:hh, :ww] - ff[:hh, :ww]) ** 2)
    assert 10 * np.log10(1 / mse) > 35


def test_save_photo_keeps_exif_and_can_strip_gps(tmp_path):
    from tests.conftest import make_photo
    p = make_photo(tmp_path / "in.jpg", 400, 300)
    src, info = render.load_photo(p)
    rgb = render.to_float(src)
    render.save_photo(rgb, tmp_path / "a.jpg", info=info)
    render.save_photo(rgb, tmp_path / "b.jpg", info=info, strip_gps=True)
    render.save_photo(rgb, tmp_path / "c.tif", fmt="tiff16", info=info)
    a, b = Image.open(tmp_path / "a.jpg").getexif(), Image.open(tmp_path / "b.jpg").getexif()
    assert a[271] == "DJI" and 0x8825 in a
    assert b[271] == "DJI" and 0x8825 not in b
    import tifffile
    t = tifffile.imread(tmp_path / "c.tif")
    assert t.dtype == np.uint16 and t.shape == (300, 400, 3)


def test_high_bit_depth_source_matches_8bit_render():
    """DNG / 16-bit frames go through float channels; result must match the 8-bit path."""
    rng = np.random.default_rng(3)
    base = (rng.random((90, 120, 3)) * 255).astype(np.uint8)
    base = np.asarray(Image.fromarray(base).resize((480, 360), Image.BICUBIC))
    src8 = render.from_array(base)
    src16 = render.from_array(base.astype(np.uint16) * 257)
    assert isinstance(src16, tuple) and src16[0].mode == "F"
    edit = {"rotate": 270, "straighten": 4, "crop": {"x": 0.1, "y": 0.2, "w": 0.7, "h": 0.6},
            "grade": {"contrast": 25, "saturation": 20}, "sharpen": 40, "vignette": -40}
    a = render.render(src8, edit)
    b = render.render(src16, edit)
    assert a.shape == b.shape
    assert np.abs(a - b).mean() < 0.01
