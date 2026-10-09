"""Still-image rendering: used for photo export, frame grabs and every live preview.

A "source" is either an 8-bit PIL RGB image (JPEG/PNG/video frames) or, for
high bit-depth material such as DNG, a tuple of three float ("F" mode) channels.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from . import grading
from .edits import Geometry, normalize_edit, plan_geometry
from .stamp import composite, render_stamp

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".dng"}
RAW_EXTS = {".dng"}
SHARPEN_MAX = 1.5    # unsharp amount at sharpen=100 (same scale as FFmpeg's unsharp)
SHARPEN_SIZE = 5     # box size of the unsharp blur (FFmpeg: luma_msize)
VIGNETTE_MAX = 0.75  # edge opacity at vignette=±100

Image.MAX_IMAGE_PIXELS = 400_000_000  # big panoramas are fine


def src_size(src) -> tuple[int, int]:
    return src.size if isinstance(src, Image.Image) else src[0].size


def _each(src, fn):
    return fn(src) if isinstance(src, Image.Image) else tuple(fn(c) for c in src)


def to_float(src) -> np.ndarray:
    if isinstance(src, Image.Image):
        return np.asarray(src.convert("RGB"), np.float32) * (1 / 255)
    return np.clip(np.stack([np.asarray(c, np.float32) for c in src], axis=-1), 0, 1)


def from_array(arr: np.ndarray):
    """uint8 [H,W,3] -> PIL RGB; uint16/float -> float channels."""
    if arr.dtype == np.uint8:
        return Image.fromarray(arr, "RGB")
    scale = 1 / 65535 if arr.dtype == np.uint16 else 1.0
    return tuple(Image.fromarray(arr[..., i].astype(np.float32) * scale, "F") for i in range(3))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_photo(path: Path, max_long_edge: int | None = None):
    """Load a photo upright. Returns (source, info) where info has exif/icc bytes."""
    path = Path(path)
    if path.suffix.lower() in TIFF_EXTS and _is_high_bit_tiff(path):
        import tifffile
        arr = tifffile.imread(path)
        if arr.ndim == 2:
            arr = np.repeat(arr[..., None], 3, axis=2)
        arr = arr[..., :3]
        src = from_array(arr.astype(np.uint16) if arr.dtype != np.uint8 else arr)
        sidecar = path.with_suffix(".exif")
        info = {"exif": sidecar.read_bytes() if sidecar.exists() else None, "icc": None}
    elif path.suffix.lower() in RAW_EXTS:
        import rawpy
        with rawpy.imread(str(path)) as raw:
            half = bool(max_long_edge and max(raw.sizes.width, raw.sizes.height) > 2 * max_long_edge)
            rgb = raw.postprocess(use_camera_wb=True, output_bps=16, half_size=half)
        src = from_array(rgb)
        info = {"exif": _exif_from_raw(path), "icc": None}
    else:
        img = Image.open(path)
        if max_long_edge:
            img.draft("RGB", (max_long_edge, max_long_edge))  # fast JPEG downscale on decode
        info = {"exif": img.info.get("exif"), "icc": img.info.get("icc_profile")}
        img = ImageOps.exif_transpose(img)
        src = img.convert("RGB") if img.mode != "RGB" else img
        src.load()
    if max_long_edge and max(src_size(src)) > max_long_edge:
        w, h = src_size(src)
        k = max_long_edge / max(w, h)
        size = (max(1, round(w * k)), max(1, round(h * k)))
        src = _each(src, lambda c: c.resize(size, Image.LANCZOS, reducing_gap=3.0))
    return src, info


TIFF_EXTS = {".tif", ".tiff"}


def _is_high_bit_tiff(path: Path) -> bool:
    """16-bit / float TIFFs (e.g. merged HDRs) need tifffile; Pillow handles 8-bit ones."""
    try:
        import tifffile
        with tifffile.TiffFile(path) as tf:
            return tf.pages[0].dtype != np.uint8
    except Exception:
        return False


def _exif_from_raw(path: Path):
    try:
        with Image.open(path) as im:  # Pillow reads the TIFF/EXIF container of most DNGs
            return im.getexif().tobytes()
    except Exception:
        return None


def photo_size(path: Path) -> tuple[int, int]:
    path = Path(path)
    if path.suffix.lower() in RAW_EXTS:
        import rawpy
        with rawpy.imread(str(path)) as raw:
            s = raw.sizes
            w, h = s.width, s.height
            return (h, w) if s.flip in (5, 6) else (w, h)
    if path.suffix.lower() in TIFF_EXTS and _is_high_bit_tiff(path):
        import tifffile
        with tifffile.TiffFile(path) as tf:
            shape = tf.pages[0].shape
        return shape[1], shape[0]
    with Image.open(path) as img:
        w, h = img.size
        orientation = img.getexif().get(274, 1)
        return (h, w) if orientation in (5, 6, 7, 8) else (w, h)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

_ROT = {90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}  # PIL rotates CCW


def orient(src, geo: Geometry):
    if geo.rotate:
        src = _each(src, lambda c: c.transpose(_ROT[geo.rotate]))
    if geo.flip_h:
        src = _each(src, lambda c: c.transpose(Image.FLIP_LEFT_RIGHT))
    if geo.flip_v:
        src = _each(src, lambda c: c.transpose(Image.FLIP_TOP_BOTTOM))
    return src


def _affine(geo: Geometry, sx: float, sy: float) -> tuple:
    """Output pixel (u, v) -> oriented-source pixel, for a crop scaled by (sx, sy)."""
    t = math.radians(geo.angle)
    c, s = math.cos(t), math.sin(t)
    ox, oy = geo.cx - geo.fw / 2, geo.cy - geo.fh / 2
    return (c * sx, s * sy, c * ox + s * oy + geo.ow / 2,
            -s * sx, c * sy, -s * ox + c * oy + geo.oh / 2)


def apply_geometry(src, geo: Geometry, out_w: int, out_h: int, fast: bool = False):
    src = orient(src, geo)
    if geo.angle == 0:
        box = (geo.cx, geo.cy, geo.cx + geo.cw, geo.cy + geo.ch)
        return _each(src, lambda ch: ch.resize((out_w, out_h), Image.LANCZOS, box=box,
                                               reducing_gap=3.0 if fast else None))
    if fast:
        data = _affine(geo, geo.cw / out_w, geo.ch / out_h)
        return _each(src, lambda ch: ch.transform((out_w, out_h), Image.AFFINE, data,
                                                  resample=Image.BICUBIC))
    nw, nh = max(1, round(geo.cw)), max(1, round(geo.ch))
    data = _affine(geo, geo.cw / nw, geo.ch / nh)
    src = _each(src, lambda ch: ch.transform((nw, nh), Image.AFFINE, data, resample=Image.BICUBIC))
    if (nw, nh) != (out_w, out_h):
        src = _each(src, lambda ch: ch.resize((out_w, out_h), Image.LANCZOS))
    return src


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------

def vignette_alpha(w: int, h: int, amount: float) -> np.ndarray:
    """Opacity of the vignette overlay (black if amount<0, white if >0)."""
    x = (np.arange(w, dtype=np.float32) + 0.5) / w * 2 - 1
    y = (np.arange(h, dtype=np.float32) + 0.5) / h * 2 - 1
    d = np.sqrt((x[None, :] ** 2 + y[:, None] ** 2) / 2)  # 0 centre .. 1 corners
    t = np.clip((d - 0.3) / 0.7, 0, 1)
    return (t * t * (3 - 2 * t)) * (abs(amount) / 100 * VIGNETTE_MAX)


def apply_vignette(rgb: np.ndarray, amount: float) -> np.ndarray:
    if not amount:
        return rgb
    a = vignette_alpha(rgb.shape[1], rgb.shape[0], amount)[..., None]
    # Quantise like the 8-bit PNG overlay FFmpeg uses, so video and photo match.
    a = np.round(a * 255) / 255
    rgb *= 1 - a
    if amount > 0:
        rgb += a
    return rgb


def vignette_overlay(w: int, h: int, amount: float) -> Image.Image:
    a = vignette_alpha(w, h, amount)
    out = np.zeros((h, w, 4), np.uint8)
    out[..., :3] = 255 if amount > 0 else 0
    out[..., 3] = np.round(a * 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


GRAD_MAX = 0.8  # top-edge opacity of the sky filter at strength 100


def grad_alpha(w: int, h: int, grad: dict) -> np.ndarray:
    """Opacity of the graduated filter: full strength at the top, gone by `height` %."""
    y = (np.arange(h, dtype=np.float32) + 0.5) / h
    t = np.clip(y / (grad["height"] / 100), 0, 1)
    col = (1 - t * t * (3 - 2 * t)) * (grad["strength"] / 100 * GRAD_MAX)
    return np.repeat(col[:, None], w, axis=1)


def apply_grad(rgb: np.ndarray, grad: dict) -> np.ndarray:
    if not grad["strength"]:
        return rgb
    a = np.round(grad_alpha(rgb.shape[1], rgb.shape[0], grad) * 255)[..., None] / 255
    c = np.array([int(grad["color"][i:i + 2], 16) for i in (1, 3, 5)], np.float32) / 255
    rgb *= 1 - a
    rgb += c * a
    return rgb


def grad_overlay(w: int, h: int, grad: dict) -> Image.Image:
    out = np.zeros((h, w, 4), np.uint8)
    out[..., :3] = [int(grad["color"][i:i + 2], 16) for i in (1, 3, 5)]
    out[..., 3] = np.round(grad_alpha(w, h, grad) * 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _box_blur(a: np.ndarray, size: int) -> np.ndarray:
    r = size // 2
    for axis in (0, 1):
        p = np.pad(a, [(r, r) if i == axis else (0, 0) for i in range(2)], mode="edge")
        c = np.cumsum(p, axis=axis, dtype=np.float32)
        c = np.concatenate([np.zeros_like(c.take([0], axis=axis)), c], axis=axis)
        n = a.shape[axis]
        a = (c.take(np.arange(size, size + n), axis=axis) - c.take(np.arange(0, n), axis=axis)) / size
    return a


def apply_sharpen(rgb: np.ndarray, amount: float) -> np.ndarray:
    """Luma unsharp mask (matches FFmpeg unsharp=5:5:amount on the Y plane)."""
    if not amount:
        return rgb
    k = amount / 100 * SHARPEN_MAX
    y = rgb @ grading.LUMA
    detail = (y - _box_blur(y, SHARPEN_SIZE)) * k
    rgb += detail[..., None]
    np.clip(rgb, 0, 1, out=rgb)
    return rgb


# ---------------------------------------------------------------------------
# The full still pipeline
# ---------------------------------------------------------------------------

def render(src, edit: dict, *, long_edge: int | None = None, mode: str = "final",
           fast: bool = False, stamp_mask: Image.Image | None = None,
           allow_upscale: bool = False) -> np.ndarray:
    """Render a still. mode="frame" returns the whole straightened frame (for the crop tool)."""
    edit = normalize_edit(edit)
    if mode == "frame":
        edit = {**edit, "crop": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}}
    w, h = src_size(src)
    geo = plan_geometry(w, h, edit)
    out_w, out_h = geo.output_size(long_edge, allow_upscale=allow_upscale)
    img = apply_geometry(src, geo, out_w, out_h, fast=fast)
    rgb = to_float(img)
    del img
    rgb = grading.grade_image(rgb, edit["grade"], n=33 if fast else 65, in_place=True)
    if mode == "final":
        apply_grad(rgb, edit["grad"])
        apply_vignette(rgb, edit["vignette"])
        apply_sharpen(rgb, edit["sharpen"])
        if edit["stamp"]["enabled"] and stamp_mask is not None:
            stamp, x, y = render_stamp(stamp_mask, out_w, out_h, edit["stamp"])
            composite(rgb, stamp, x, y)
    return rgb


SLIDE_W, SLIDE_H = 1080, 1350  # Instagram portrait (4:5)
MAX_SLIDES = 20


def carousel_layout(cw: float, ch: float) -> tuple[int, tuple[int, int]]:
    """Number of slides and the render size that covers n*1080 x 1350 for a cw x ch crop."""
    n = int(np.clip(round(cw / ch * SLIDE_H / SLIDE_W), 2, MAX_SLIDES))
    k = max(n * SLIDE_W / cw, SLIDE_H / ch)
    return n, (max(n * SLIDE_W, round(cw * k)), max(SLIDE_H, round(ch * k)))


def render_carousel(src, edit: dict, stamp_mask=None) -> list[np.ndarray]:
    """Split a (panorama) crop into seamless 4:5 slides; the stamp goes on the last one."""
    edit = normalize_edit(edit)
    w, h = src_size(src)
    geo = plan_geometry(w, h, edit)
    n, box = carousel_layout(geo.cw, geo.ch)
    rgb = render(src, {**edit, "stamp": {**edit["stamp"], "enabled": False}}, long_edge=box,
                 allow_upscale=True)
    H, W = rgb.shape[:2]
    x0, y0 = (W - n * SLIDE_W) // 2, (H - SLIDE_H) // 2
    strip = rgb[y0:y0 + SLIDE_H, x0:x0 + n * SLIDE_W]
    slides = [np.ascontiguousarray(strip[:, i * SLIDE_W:(i + 1) * SLIDE_W]) for i in range(n)]
    if edit["stamp"]["enabled"] and stamp_mask is not None:
        st, x, y = render_stamp(stamp_mask, SLIDE_W, SLIDE_H, edit["stamp"])
        composite(slides[-1], st, x, y)
    return slides


def to_uint8(rgb: np.ndarray) -> np.ndarray:
    return (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)


def encode_jpeg(rgb: np.ndarray, quality: int = 85) -> bytes:
    import io
    buf = io.BytesIO()
    Image.fromarray(to_uint8(rgb), "RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def _prepare_exif(exif_bytes, out_w: int, out_h: int, strip_gps: bool):
    exif = Image.Exif()
    if exif_bytes:
        try:
            exif.load(exif_bytes)
        except Exception:
            exif = Image.Exif()
    exif[274] = 1                     # we bake orientation into the pixels
    exif[305] = "SirisDrone Studio"   # Software
    try:
        ifd = exif.get_ifd(0x8769)
        ifd[0xA002], ifd[0xA003] = out_w, out_h  # PixelXDimension / PixelYDimension
    except Exception:
        pass
    if strip_gps and 0x8825 in exif:
        del exif[0x8825]
    return exif


PHOTO_FORMATS = {"jpeg": ".jpg", "png": ".png", "tiff16": ".tif"}


def save_photo(rgb: np.ndarray, path: Path, fmt: str = "jpeg", quality: int = 95,
               info: dict | None = None, keep_metadata: bool = True, strip_gps: bool = False) -> None:
    info = info or {}
    h, w = rgb.shape[:2]
    exif = _prepare_exif(info.get("exif"), w, h, strip_gps) if keep_metadata else None
    icc = info.get("icc") if keep_metadata else None
    if fmt == "tiff16":
        import tifffile
        data = (np.clip(rgb, 0, 1) * 65535 + 0.5).astype(np.uint16)
        tifffile.imwrite(path, data, photometric="rgb", compression="zlib",
                         software="SirisDrone Studio", iccprofile=icc)
        return
    img = Image.fromarray(to_uint8(rgb), "RGB")
    kwargs = {}
    if exif is not None:
        kwargs["exif"] = exif
    if icc:
        kwargs["icc_profile"] = icc
    if fmt == "png":
        img.save(path, "PNG", compress_level=6, **kwargs)
    else:
        q = int(min(max(quality, 50), 100))
        img.save(path, "JPEG", quality=q, subsampling=0 if q >= 90 else 2, optimize=True, **kwargs)
