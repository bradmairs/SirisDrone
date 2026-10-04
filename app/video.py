"""Video: probing, preview frames and FFmpeg export.

The export filter graph mirrors render.render() step for step:
geometry -> resize -> colour LUT -> vignette overlay -> luma sharpen -> stamp overlay.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from . import grading
from .edits import normalize_edit, plan_geometry
from .render import SHARPEN_MAX, SHARPEN_SIZE, vignette_overlay
from .stamp import render_stamp

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts"}
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

_MATRIX = {"bt709": "bt709", "smpte170m": "bt601", "bt470bg": "bt601", "bt2020nc": "bt2020",
           "bt2020c": "bt2020", "fcc": "fcc", "smpte240m": "smpte240m"}

CODECS = {
    # name: (label, container ext, ffmpeg args, output pix_fmt)
    "h264": ("H.264 (MP4) – plays everywhere", ".mp4",
             ["-c:v", "libx264", "-preset", "slow", "-profile:v", "high"], "yuv420p"),
    "h265": ("H.265 / HEVC 10-bit (MP4) – smaller files", ".mp4",
             ["-c:v", "libx265", "-preset", "medium", "-tag:v", "hvc1"], "yuv420p10le"),
    "prores": ("ProRes 422 HQ (MOV) – for further editing", ".mov",
               ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0"], "yuv422p10le"),
}
QUALITY_CRF = {"h264": {"max": 14, "high": 17, "standard": 21},
               "h265": {"max": 16, "high": 19, "standard": 23}}


class FFmpegError(RuntimeError):
    pass


def probe(path: Path) -> dict:
    res = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams",
                          "-show_format", str(path)], capture_output=True, text=True)
    if res.returncode != 0:
        raise FFmpegError(f"Could not read video: {res.stderr.strip()[-300:]}")
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    if not v:
        raise FFmpegError("No video stream found")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int(float((v.get("tags") or {}).get("rotate", 0) or 0))
    if abs(rot) % 180 == 90:
        w, h = h, w
    num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    if not fps or fps > 1000:
        num, _, den = (v.get("r_frame_rate") or "30/1").partition("/")
        fps = float(num) / float(den or 1)
    duration = float(v.get("duration") or data.get("format", {}).get("duration") or 0)
    pix = v.get("pix_fmt", "")
    return {
        "width": w, "height": h, "duration": duration, "fps": round(fps, 3),
        "codec": v.get("codec_name"), "pix_fmt": pix,
        "bit_depth": 10 if ("10" in pix or "12" in pix) else 8,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "color_space": v.get("color_space"), "color_range": v.get("color_range"),
        "color_transfer": v.get("color_transfer"),
        "creation_time": (data.get("format", {}).get("tags") or {}).get("creation_time"),
    }


def _input_conv(meta: dict) -> str:
    matrix = _MATRIX.get(meta.get("color_space") or "", "bt709")
    rng = "pc" if meta.get("color_range") in ("pc", "jpeg") else "tv"
    return f"in_color_matrix={matrix}:in_range={rng}"


def extract_frame(path: Path, meta: dict, t: float, long_edge: int | None = None,
                  bits: int = 8) -> np.ndarray:
    """Decode one frame as RGB (uint8, or uint16 when bits=16)."""
    w, h = meta["width"], meta["height"]
    if long_edge and max(w, h) > long_edge:
        k = long_edge / max(w, h)
        w, h = max(2, round(w * k)), max(2, round(h * k))
    t = max(0.0, min(t, max(meta["duration"] - 0.05, 0)))
    pix = "rgb48le" if bits == 16 else "rgb24"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{t:.3f}",
           "-i", str(path), "-frames:v", "1", "-an", "-sn",
           "-vf", f"scale={w}:{h}:flags=lanczos+accurate_rnd+full_chroma_int:{_input_conv(meta)},format={pix}",
           "-f", "rawvideo", "-pix_fmt", pix, "pipe:1"]
    res = subprocess.run(cmd, capture_output=True)
    dtype = np.uint16 if bits == 16 else np.uint8
    need = w * h * 3 * (2 if bits == 16 else 1)
    if res.returncode != 0 or len(res.stdout) < need:
        if t > 0.5:  # some files end early; try a little earlier
            return extract_frame(path, meta, t - 0.5, long_edge, bits)
        raise FFmpegError(f"Could not decode frame: {res.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(res.stdout[:need], dtype=dtype).reshape(h, w, 3)


def _atempo(speed: float) -> str:
    parts = []
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    parts.append(f"atempo={speed:.6f}")
    return ",".join(parts)


def _esc(path: Path) -> str:
    """Escape a path for use inside a filtergraph option value."""
    s = str(path).replace("\\", "/")
    return s.replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")


def build_graph(meta: dict, edit: dict, workdir: Path, stamp_mask: Image.Image | None,
                long_edge: int | None = None, fps: float | None = None,
                out_pix_fmt: str = "yuv420p", allow_upscale: bool = False) -> dict:
    """Return the FFmpeg input args / filter graph for an edit (without encoder settings)."""
    edit = normalize_edit(edit)
    geo = plan_geometry(meta["width"], meta["height"], edit)
    out_w, out_h = geo.output_size(long_edge, even=True, allow_upscale=allow_upscale)
    v = edit["video"]
    inputs: list[str] = []
    chain: list[str] = []

    speed = v["speed"]
    if speed != 1:
        chain.append(f"setpts=PTS/{speed:.6f}")
    if fps:
        chain.append(f"fps={fps}")

    # 1. orientation
    if geo.rotate == 90:
        chain.append("transpose=clock")
    elif geo.rotate == 270:
        chain.append("transpose=cclock")
    elif geo.rotate == 180:
        chain += ["hflip", "vflip"]
    if geo.flip_h:
        chain.append("hflip")
    if geo.flip_v:
        chain.append("vflip")
    # 2. straighten (FFmpeg's rotate is clockwise for positive angles, like ours)
    if geo.angle:
        chain.append(f"rotate={math.radians(geo.angle):.8f}:ow=iw:oh=ih:c=black")
    # 3. crop (even values keep 4:2:0 chroma aligned)
    offx, offy = geo.frame_offset
    cw = max(2, int(round(geo.cw)) // 2 * 2)
    ch = max(2, int(round(geo.ch)) // 2 * 2)
    cx = int(round(offx + geo.cx)) // 2 * 2
    cy = int(round(offy + geo.cy)) // 2 * 2
    if (cw, ch) != (geo.ow, geo.oh) or cx or cy:
        chain.append(f"crop={cw}:{ch}:{cx}:{cy}")
    # 4. resize + convert to RGB using the source's real colour matrix
    chain.append(f"scale={out_w}:{out_h}:flags=lanczos+accurate_rnd+full_chroma_int:{_input_conv(meta)}")
    chain.append("format=gbrp16le")
    # 5. colour grade
    if not grading.is_neutral(edit["grade"]):
        lut_path = workdir / "grade.cube"
        grading.write_cube(lut_path, grading.build_lut(edit["grade"], 65))
        chain.append(f"lut3d=file='{_esc(lut_path)}':interp=trilinear")
    chain.append("scale=out_color_matrix=bt709:out_range=tv")
    chain.append("format=yuv444p10le")

    graph = [f"[0:v]{','.join(chain)}[g0]"]
    last, n_in = "g0", 1

    def overlay(img: Image.Image, name: str, x: int, y: int):
        nonlocal last, n_in
        p = workdir / f"{name}.png"
        img.save(p)
        inputs.extend(["-i", str(p)])
        graph.append(f"[{n_in}:v]scale=out_color_matrix=bt709:out_range=tv,format=yuva444p10le[{name}]")
        graph.append(f"[{last}][{name}]overlay={x}:{y}:format=yuv444p10:eof_action=repeat[{name}o]")
        last = f"{name}o"
        n_in += 1

    # 6. vignette, 7. sharpen, 8. stamp
    if edit["vignette"]:
        overlay(vignette_overlay(out_w, out_h, edit["vignette"]), "vig", 0, 0)
    if edit["sharpen"]:
        amt = edit["sharpen"] / 100 * SHARPEN_MAX
        graph.append(f"[{last}]unsharp={SHARPEN_SIZE}:{SHARPEN_SIZE}:{amt:.4f}:3:3:0[sh]")
        last = "sh"
    if edit["stamp"]["enabled"] and stamp_mask is not None:
        img, x, y = render_stamp(stamp_mask, out_w, out_h, edit["stamp"])
        overlay(img, "stamp", x, y)
    graph.append(f"[{last}]format={out_pix_fmt}[vout]")

    start = v["trim_start"]
    end = v["trim_end"] if v["trim_end"] is not None else meta["duration"]
    end = min(end, meta["duration"]) if meta["duration"] else end
    src_dur = max(0.04, end - start)
    return {
        "inputs": inputs, "filter": ";".join(graph), "width": out_w, "height": out_h,
        "start": start, "src_duration": src_dur, "duration": src_dur / speed,
        "speed": speed, "mute": v["mute"],
    }


def export_command(src: Path, meta: dict, g: dict, out_path: Path, codec: str = "h264",
                   quality: str = "high", keep_metadata: bool = True) -> list[str]:
    label, ext, vargs, pix = CODECS[codec]
    cmd = [FFMPEG, "-hide_banner", "-y", "-nostdin", "-loglevel", "error",
           "-ss", f"{g['start']:.3f}", "-t", f"{g['src_duration']:.3f}", "-i", str(src),
           *g["inputs"], "-filter_complex", g["filter"], "-map", "[vout]", *vargs]
    if codec in QUALITY_CRF:
        cmd += ["-crf", str(QUALITY_CRF[codec].get(quality, QUALITY_CRF[codec]["high"]))]
    cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-color_range", "tv"]
    if meta.get("has_audio") and not g["mute"]:
        cmd += ["-map", "0:a:0?"]
        if g["speed"] != 1:
            cmd += ["-af", _atempo(g["speed"])]
        cmd += ["-c:a", "pcm_s16le"] if codec == "prores" else ["-c:a", "aac", "-b:a", "320k"]
    cmd += ["-map_metadata", "0" if keep_metadata else "-1", "-map_chapters", "-1"]
    if ext == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", "-f", "mov" if ext == ".mov" else "mp4", str(out_path)]
    return cmd


def run_ffmpeg(cmd: list[str], duration: float, on_progress: Callable[[float], None] | None = None,
               cancel: threading.Event | None = None) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            bufsize=1)
    err_tail: deque[str] = deque(maxlen=30)
    t = threading.Thread(target=lambda: err_tail.extend(proc.stderr), daemon=True)
    t.start()
    try:
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                break
            key, _, val = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and val.isdigit() and on_progress and duration:
                on_progress(min(int(val) / 1e6 / duration, 0.999))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    t.join(timeout=2)
    if cancel is not None and cancel.is_set():
        raise InterruptedError("cancelled")
    if proc.returncode != 0:
        raise FFmpegError("FFmpeg failed: " + "".join(err_tail).strip()[-800:])
