"""DJI flight telemetry from the .SRT subtitle file saved beside each clip.

The Mini 3 Pro writes one subtitle per frame, e.g.::

    1
    00:00:00,000 --> 00:00:00,033
    <font size="28">FrameCnt: 1, DiffTime: 33ms
    2026-04-03 10:12:31.123
    [iso: 100] [shutter: 1/1000.0] [fnum: 1.7] [ev: 0] [color_md: default] [focal_len: 24.00]
    [latitude: -38.123456] [longitude: 145.123456] [rel_alt: 50.300 abs_alt: 120.500] [ct: 5500] </font>

Older DJI drones (Mini 2) write ``GPS(lon, lat, alt) ... H 50.3m H.S 5.2m/s``; both are read.
Speed and distance aren't in the Mini 3 Pro's file, so they are worked out from the positions:
horizontal speed over about a second, and straight-line distance from where the clip started.
"""
from __future__ import annotations

import functools
import math
import re
from pathlib import Path

_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->")
_NUM = r"(-?\d+(?:\.\d+)?)"
_FIELDS = {
    "lat": re.compile(r"latitude\s*:\s*" + _NUM),
    "lon": re.compile(r"long(?:i|t)tude\s*:\s*" + _NUM),
    "alt": re.compile(r"rel_alt\s*:\s*" + _NUM),
    "abs_alt": re.compile(r"abs_alt\s*:\s*" + _NUM),
    "iso": re.compile(r"iso\s*:\s*" + _NUM),
    "fnum": re.compile(r"fnum\s*:\s*" + _NUM),
    "ev": re.compile(r"\bev\s*:\s*" + _NUM),
    "ct": re.compile(r"\bct\s*:\s*" + _NUM),
}
_SHUTTER = re.compile(r"shutter\s*:\s*([\d./]+)")
_OLD_GPS = re.compile(r"GPS\s*\(\s*" + _NUM + r"\s*,\s*" + _NUM + r"(?:\s*,\s*" + _NUM + r")?")
_OLD_H = re.compile(r"\bH\s+" + _NUM + r"\s*m\b")
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)")


def srt_for(video_path: Path) -> Path | None:
    p = Path(video_path)
    for srt in (p.with_suffix(".SRT"), p.with_suffix(".srt")):
        if srt.exists():
            return srt
    return None


def parse(text: str) -> list[dict]:
    """One dict per subtitle: t (seconds into the clip) and whatever the drone recorded."""
    out: list[dict] = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        m = _TIME.search(block)
        if not m:
            continue
        h, mi, s, ms = m.groups()
        row: dict = {"t": int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000}
        for key, rx in _FIELDS.items():
            f = rx.search(block)
            if f:
                row[key] = float(f.group(1))
        sh = _SHUTTER.search(block)
        if sh:
            row["shutter"] = sh.group(1)
        old = _OLD_GPS.search(block)
        if old and "lat" not in row:
            row["lon"], row["lat"] = float(old.group(1)), float(old.group(2))
        oh = _OLD_H.search(block)
        if oh and "alt" not in row:
            row["alt"] = float(oh.group(1))
        st = _STAMP.search(block)
        if st:
            row["time"] = st.group(1)
        if row.get("lat") == 0 and row.get("lon") == 0:   # no GPS fix yet
            row.pop("lat"), row.pop("lon")
        out.append(row)
    return out


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres between two (lat, lon) points."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(min(1.0, math.sqrt(h)))


def enrich(rows: list[dict], window: float = 1.0) -> list[dict]:
    """Add speed (m/s, horizontal, over ~`window` s), vspeed (m/s) and dist (m from the start)."""
    fixes = [r for r in rows if "lat" in r]
    if not fixes:
        return rows
    home = (fixes[0]["lat"], fixes[0]["lon"])
    j = 0
    for i, r in enumerate(fixes):
        while j < i and r["t"] - fixes[j]["t"] > window:
            j += 1
        a = fixes[j]
        dt = r["t"] - a["t"]
        r["dist"] = haversine(home, (r["lat"], r["lon"]))
        if dt > 0.2:
            r["speed"] = haversine((a["lat"], a["lon"]), (r["lat"], r["lon"])) / dt
            if "alt" in r and "alt" in a:
                r["vspeed"] = (r["alt"] - a["alt"]) / dt
    for i, r in enumerate(fixes):  # the first second has no look-back: borrow the next value
        if "speed" not in r:
            nxt = next((f for f in fixes[i:] if "speed" in f), None)
            r["speed"] = nxt["speed"] if nxt else 0.0
            r["vspeed"] = nxt.get("vspeed", 0.0) if nxt else 0.0
    return rows


@functools.lru_cache(maxsize=64)
def _load(path: str, mtime: float) -> tuple:
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return ()
    return tuple(enrich(parse(text)))


def load(video_path: Path) -> list[dict]:
    """Telemetry for a clip, [] when it has no .SRT."""
    srt = srt_for(video_path)
    if srt is None:
        return []
    return [dict(r) for r in _load(str(srt), srt.stat().st_mtime)]


def at(rows: list[dict], t: float) -> dict | None:
    """The sample in effect at `t` seconds into the clip."""
    best = None
    for r in rows:
        if r["t"] <= t + 1e-6:
            best = r
        else:
            break
    return best or (rows[0] if rows else None)


def track(rows: list[dict], step: float = 1.0) -> list[list[float]]:
    """[[lat, lon], ...] about every `step` seconds, for drawing the flight path."""
    out, last = [], -1e9
    for r in rows:
        if "lat" in r and r["t"] - last >= step:
            out.append([round(r["lat"], 6), round(r["lon"], 6)])
            last = r["t"]
    return out


def summary(rows: list[dict]) -> dict:
    fixes = [r for r in rows if "lat" in r]
    alts = [r["alt"] for r in rows if "alt" in r]
    path = sum(haversine((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(fixes, fixes[1:]))
    return {"samples": len(rows), "duration": rows[-1]["t"] if rows else 0.0,
            "max_alt": max(alts) if alts else None,
            "max_speed": max((r.get("speed", 0) for r in fixes), default=None),
            "max_dist": max((r.get("dist", 0) for r in fixes), default=None),
            "path_m": round(path, 1)}
