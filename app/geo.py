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
_lock_http = threading.Lock()
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
    return gps_from_iso6709(meta.get("location")) or gps_from_srt(Path(path)) or gps_from_nearby_photo(Path(path))


_DJI_NUMBER = re.compile(r"DJI_(\d+)", re.I)
_NEARBY_LIMIT = 50  # file numbers: further away is probably another flight


def gps_from_nearby_photo(path: Path) -> tuple[float, float] | None:
    """GPS of the photo taken closest to a video (by DJI file number) in the same folder, or
    in sibling folders when the video sits in a Videos/ folder next to JPEG/ and DNG/. For
    videos that carry no GPS themselves (no metadata location and no .SRT file)."""
    m = _DJI_NUMBER.search(path.stem)
    if not m:
        return None
    number = int(m.group(1))
    dirs = [path.parent]
    if path.parent.name.lower() in ("videos", "video", "mp4", "movies"):
        dirs += [d for d in path.parent.parent.iterdir() if d.is_dir() and d != path.parent]
        dirs.append(path.parent.parent)
    photos = []
    for d in dirs:
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            pm = _DJI_NUMBER.search(p.stem)
            if pm and p.suffix.lower() in (".jpg", ".jpeg", ".dng") and abs(int(pm.group(1)) - number) <= _NEARBY_LIMIT:
                photos.append((abs(int(pm.group(1)) - number), p.suffix.lower() == ".dng", p))
    for _, _, p in sorted(photos):
        try:
            with Image.open(p) as im:
                gps = gps_from_exif(im.getexif().tobytes())
        except Exception:
            continue
        if gps:
            return gps
    return None


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


# Nearest named place first: drone shots are mostly outside big cities, and "Christmas Hills"
# says more than "Melbourne". County/state only when no place is found nearby.
_CITY_KEYS = ("town", "village", "suburb", "city_district", "hamlet", "city",
              "municipality")  # not "locality": often a park
_FALLBACK_KEYS = ("island", "county", "state")  # "Phillip Island" beats "Victoria"
# Over the sea or a beach Nominatim often has only the state; nudging the point towards land
# finds the nearest town (2 km, then 5 km away).
_NUDGES = ((0, 0), (0.02, 0), (-0.02, 0), (0, 0.02), (0, -0.02),
           (0.05, 0), (-0.05, 0), (0, 0.05), (0, -0.05))


def _lookup_address(lat: float, lon: float) -> dict:
    """Nominatim address in English, detailed enough (zoom 16) to name villages."""
    global _last_call
    url = os.environ.get("SIRIS_GEOCODER") or DEFAULT_URL
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
                                "zoom": 16, "addressdetails": 1, "accept-language": "en"})
    with _lock_http:
        wait = 1.05 - (time.time() - _last_call)  # Nominatim: at most one request a second
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        req = urllib.request.Request(f"{url}?{q}", headers={
            "User-Agent": "SirisDrone-Studio/1.0 (self-hosted drone photo editor)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("address") or {}


def city_country(gps: tuple[float, float] | None) -> str:
    """"City, Country" for a GPS position (e.g. "Cowes, Australia"), or "" when there is no
    GPS, no connection or lookups are off. Cached like place_name."""
    if not gps or os.environ.get("SIRIS_GEOCODER", "").lower() in ("off", "0", "false"):
        return ""
    # "cc2": results cached before 1.14 (no districts/islands) are looked up again.
    key = f"cc2:{gps[0]:.2f},{gps[1]:.2f}"  # ~1 km: plenty for a city name
    with _lock:
        cache = _load_cache()
        if key in cache:
            return cache[key]
    city, country, fallback = "", "", ""
    try:
        for dlat, dlon in _NUDGES:
            addr = _lookup_address(gps[0] + dlat, gps[1] + dlon)
            country = country or str(addr.get("country") or "")
            fallback = fallback or next((str(addr[k]) for k in _FALLBACK_KEYS if addr.get(k)), "")
            city = next((str(addr[k]) for k in _CITY_KEYS if addr.get(k)), "")
            if city:
                break
    except Exception:
        return ""  # offline: try again next time
    text = ", ".join(p for p in (city or fallback, country) if p)[:80]
    with _lock:
        cache = _load_cache()
        cache[key] = text
        if _cache_file:
            try:
                _cache_file.write_text(json.dumps(cache, indent=0))
            except OSError:
                pass
    return text


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
