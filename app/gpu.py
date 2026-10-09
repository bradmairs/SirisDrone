"""NVIDIA GPU support (NVENC encoding, NVDEC/CUDA decoding) with automatic CPU fallback.

Detection runs real tiny FFmpeg encodes/decodes, so it only reports the GPU as
usable when the driver, the container runtime and FFmpeg all work together.
Set SIRIS_GPU=off to force the CPU.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

_lock = threading.Lock()
_status: dict | None = None

# NVENC constant-quality values per quality level (lower = better).
NVENC_CQ = {"h264": {"max": 16, "high": 19, "standard": 23},
            "h265": {"max": 18, "high": 21, "standard": 25}}


def _run(cmd: list[str], timeout: float = 30) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _gpu_name() -> str | None:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out.splitlines()[0] if out else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def detect(force: bool = False) -> dict:
    """{"available", "h264", "hevc", "decode", "name", "reason"} – cached after the first call."""
    global _status
    with _lock:
        if _status is not None and not force:
            return _status
        st = {"available": False, "h264": False, "hevc": False, "decode": False,
              "name": None, "reason": ""}
        if os.environ.get("SIRIS_GPU", "auto").lower() in ("off", "0", "false", "cpu"):
            st["reason"] = "disabled with SIRIS_GPU=off"
            _status = st
            return st
        base = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "testsrc2=size=640x360:rate=30:duration=0.3"]
        st["h264"] = _run(base + ["-c:v", "h264_nvenc", "-f", "null", "-"])
        with tempfile.TemporaryDirectory() as d:
            clip = Path(d) / "t.mp4"
            st["hevc"] = _run(base + ["-pix_fmt", "p010le", "-c:v", "hevc_nvenc",
                                      "-profile:v", "main10", str(clip)])
            if st["hevc"]:
                st["decode"] = _run([FFMPEG, "-hide_banner", "-loglevel", "error",
                                     "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                                     "-i", str(clip), "-f", "null", "-"])
        st["available"] = st["h264"] or st["hevc"]
        if st["available"]:
            st["name"] = _gpu_name() or "NVIDIA GPU"
        else:
            st["reason"] = ("no NVIDIA GPU visible to FFmpeg (check the driver and, in Docker, "
                            "the NVIDIA Container Toolkit with NVIDIA_DRIVER_CAPABILITIES=compute,video,utility)")
        _status = st
        return st


def set_status_for_tests(st: dict | None) -> None:
    global _status
    with _lock:
        _status = st


def encoder(codec: str, quality: str, use_gpu: bool) -> tuple[list[str], str, bool] | None:
    """NVENC args for h264/h265 as (args, pix_fmt, True), or None to use the CPU encoder."""
    if not use_gpu or codec not in NVENC_CQ:
        return None
    st = detect()
    cq = str(NVENC_CQ[codec].get(quality, NVENC_CQ[codec]["high"]))
    common = ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", cq, "-b:v", "0",
              "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32"]
    if codec == "h264" and st["h264"]:
        return ["-c:v", "h264_nvenc", "-profile:v", "high", *common], "yuv420p", True
    if codec == "h265" and st["hevc"]:
        return ["-c:v", "hevc_nvenc", "-profile:v", "main10", *common, "-tag:v", "hvc1"], "p010le", True
    return None


def decode_args(use_gpu: bool) -> list[str]:
    """Input options for GPU decoding (frames come back to system memory for the CPU filters)."""
    if use_gpu and detect()["decode"]:
        return ["-hwaccel", "cuda"]
    return []
