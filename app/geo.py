"""Place names for the {place} file-name token.

GPS comes from photo EXIF, the video's metadata, or the DJI .SRT file saved next to a
clip. Names come from OpenStreetMap's Nominatim reverse geocoder (results are cached on
disk, and requests are limited to one per second as its usage policy asks).

SIRIS_GEOCODER=off disables lookups; SIRIS_GEOCODER=<url> points at your own Nominatim.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

DEFAULT_URL = "https://nominatim.openstreetmap.org/reverse"
_ADDRESS_ORDER = ("natural", "beach", "bay", "peak", "island", "tourism", "leisure", "locality",
                  "hamlet", "village", "suburb", "town", "city", "municipality", "county", "state")

_lock = threading.Lock()
_last_call = 0.0
_cache: dict | None = None
_cache_file: Path | None = None


def configure(cache_file: Path) -> None:
    global _cache_file, _cache
    _cache_file, _cache = cache_file, None


# ---------------------------------------------------------------------------
# GPS extraction
# ---------------------------------------------------------------------------

def _dms(v) -> float:
    d, m, s = (float(x) for x in v)
    return d + m / 60 + s / 3600


def gps_from_exif(exif_bytes) -> tuple[float, float] | None:
    if not exif_bytes:
        return None
    try:
        ex = Image.Exif()
        ex.load(exif_bytes)
        g = ex.get_ifd(0x8825)
        lat, lon = _dms(g[2]), _dms(g[4])
        if str(g.get(1, "N")).upper().startswith("S"):
            lat = -lat
        if str(g.get(3, "E")).upper().startswith("W"):
            lon = -lon
        return (lat, lon) if (lat or lon) else None
    except Exception:
        return None


def gps_from_iso6709(value: str | None) -> tuple[float, float] | None:
    m = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", value or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def gps_from_srt(video_path: Path) -> tuple[float, float] | None:
    """DJI writes a .SRT next to each clip with per-frame telemetry."""
    for srt in (video_path.with_suffix(".SRT"), video_path.with_suffix(".srt")):
        if not srt.exists():
            continue
        try:
            text = srt.read_text(errors="replace")[:20000]
        except OSError:
            return None
        m = re.search(r"latitude\s*:\s*(-?\d+\.\d+)\]?\s*\[?\s*long(?:i|t)tude\s*:\s*(-?\d+\.\d+)", text)
        if m:
            return float(m.group(1)), float(m.group(2))
        m = re.search(r"GPS\s*\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", text)  # GPS(lon, lat, alt)
        if m:
            return float(m.group(2)), float(m.group(1))
    return None


def gimbal_pitch(path: Path) -> float | None:
    """Camera tilt in degrees (-90 = straight down) from DJI photo XMP or the clip's .SRT."""
    path = Path(path)
    try:
        if path.suffix.lower() in (".mp4", ".mov", ".m4v"):
            for srt in (path.with_suffix(".SRT"), path.with_suffix(".srt")):
                if srt.exists():
                    m = re.search(r"gb_pitch\s*:?\s*(-?\d+(?:\.\d+)?)", srt.read_text(errors="replace")[:20000])
                    return float(m.group(1)) if m else None
            return None
        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
        m = re.search(rb'GimbalPitchDegree\s*=\s*"([-+]?\d+(?:\.\d+)?)"', head) or \
            re.search(rb"<drone-dji:GimbalPitchDegree>([-+]?\d+(?:\.\d+)?)<", head)
        return float(m.group(1)) if m else None
    except OSError:
        return None


def gps_for_video(path: Path, meta: dict) -> tuple[float, float] | None:
    return gps_from_iso6709(meta.get("location")) or gps_from_srt(Path(path))


# ---------------------------------------------------------------------------
# Reverse geocoding
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    global _cache
    if _cache is None:
        _cache = {}
        if _cache_file and _cache_file.exists():
            try:
                _cache = json.loads(_cache_file.read_text())
            except ValueError:
                _cache = {}
    return _cache


def _lookup(lat: float, lon: float) -> str:
    """Ask Nominatim for a short place name. Returns "" when nothing useful comes back."""
    global _last_call
    url = os.environ.get("SIRIS_GEOCODER") or DEFAULT_URL
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
                                "zoom": 14, "addressdetails": 1})
    wait = 1.05 - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    req = urllib.request.Request(f"{url}?{q}", headers={
        "User-Agent": "SirisDrone-Studio/1.0 (self-hosted drone photo editor)"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode())
    addr = data.get("address") or {}
    for key in _ADDRESS_ORDER:
        if addr.get(key):
            return str(addr[key])
    return str(data.get("name") or "")


def place_name(gps: tuple[float, float] | None) -> str:
    """Short place name for a GPS position, or "" (no GPS, offline, or lookups disabled)."""
    if not gps or os.environ.get("SIRIS_GEOCODER", "").lower() in ("off", "0", "false"):
        return ""
    key = f"{gps[0]:.3f},{gps[1]:.3f}"  # ~100 m: shots from one flight share a lookup
    with _lock:
        cache = _load_cache()
        if key in cache:
            return cache[key]
        try:
            name = _lookup(*gps)
        except Exception:
            return ""  # offline: try again next time
        cache[key] = re.sub(r'[\\/:*?"<>|]', "", name).strip()[:60]
        if _cache_file:
            try:
                _cache_file.write_text(json.dumps(cache, indent=0))
            except OSError:
                pass
        return cache[key]
