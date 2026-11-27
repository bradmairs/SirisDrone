"""Flight log: every flight, worked out from the footage.

A flight is a run of clips (and the photos taken among them) with no gap longer than
GAP_MINUTES: landing, swapping the battery and taking off again takes longer than that, so a
longer gap starts a new flight. Each clip's DJI .SRT gives its start time, path, height and
speed (see telemetry.py); photos join the flight they were taken during by their EXIF time.

For each flight: when and where (the place the editor found), how long (first frame to last,
so the airtime is at least this), the furthest from the take-off point, highest, fastest, the
distance flown, and what was shot. Totals: flights, hours, kilometres, the highest and furthest
ever, and the places. A note can be kept per flight (the battery, the wind, who was there).

Clips without an .SRT have no time or position, so they aren't in the log.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

from . import telemetry

GAP_MINUTES = 6


def _clip_start(rows: list[dict]) -> float | None:
    stamp = next((r["time"] for r in rows if r.get("time")), None)
    if not stamp:
        return None
    try:
        return dt.datetime.fromisoformat(stamp.replace(" ", "T")[:26]).timestamp()
    except ValueError:
        return None


def _segments(lib) -> list[dict]:
    """One entry per clip with telemetry, and per photo with a capture time."""
    out = []
    for it in lib.list():
        if it["kind"] == "video":
            rows = lib.flight(it)
            start = _clip_start(rows) if rows else None
            if start is None:
                continue
            fixes = [r for r in rows if "lat" in r]
            s = telemetry.summary(rows)
            out.append({"id": it["id"], "kind": "video", "name": it["name"], "start": start, "end": start + s["duration"],
                        "first": (fixes[0]["lat"], fixes[0]["lon"]) if fixes else None,
                        "fixes": [(r["lat"], r["lon"]) for r in fixes[:: max(1, len(fixes) // 400)]],
                        "max_alt": s["max_alt"], "max_speed": s["max_speed"], "path_m": s["path_m"]})
        elif it["kind"] == "photo":
            t = (it.get("cull_features") or {}).get("time")
            if t:
                out.append({"id": it["id"], "kind": "photo", "name": it["name"], "start": t, "end": t,
                            "first": tuple(it["gps"]) if it.get("gps") else None, "fixes": [tuple(it["gps"])] if it.get("gps") else []})
    return sorted(out, key=lambda s: s["start"])


def _place(lib, ids: list[str]) -> str:
    for mid in ids:
        try:
            text = (lib.get_edit(mid).get("location") or {}).get("text")
        except Exception:  # noqa: BLE001 - a removed item
            text = None
        if text:
            return text
    return ""


def flights(lib, notes: dict | None = None) -> list[dict]:
    """Newest first."""
    notes = notes or {}
    groups: list[list[dict]] = []
    for seg in _segments(lib):
        if groups and seg["start"] - max(s["end"] for s in groups[-1]) <= GAP_MINUTES * 60:
            groups[-1].append(seg)
        else:
            groups.append([seg])
    out = []
    for g in groups:
        clips = [s for s in g if s["kind"] == "video"]
        if not clips:
            continue  # photos alone: likely not a drone flight we can place in time
        start, end = min(s["start"] for s in g), max(s["end"] for s in g)
        home = next((s["first"] for s in g if s["first"]), None)
        far = max((telemetry.haversine(home, p) for s in g for p in s["fixes"]), default=None) if home else None
        alts = [s["max_alt"] for s in clips if s.get("max_alt") is not None]
        speeds = [s["max_speed"] for s in clips if s.get("max_speed") is not None]
        fid = dt.datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M%S")
        out.append({
            "id": fid, "start": dt.datetime.fromtimestamp(start).isoformat(timespec="seconds"),
            "end": dt.datetime.fromtimestamp(end).isoformat(timespec="seconds"),
            "minutes": round((end - start) / 60, 1), "place": _place(lib, [s["id"] for s in g]),
            "home": list(home) if home else None,
            "max_alt_m": round(max(alts), 1) if alts else None, "max_speed_kmh": round(max(speeds) * 3.6, 1) if speeds else None,
            "furthest_m": round(far) if far is not None else None, "flown_km": round(sum(s.get("path_m") or 0 for s in clips) / 1000, 2),
            "clips": len(clips), "photos": len(g) - len(clips), "items": [s["id"] for s in g],
            "note": notes.get(fid, ""),
        })
    return sorted(out, key=lambda f: f["start"], reverse=True)


def totals(log: list[dict], year: int | None = None) -> dict:
    year = year or dt.date.today().year
    this_year = [f for f in log if f["start"][:4] == str(year)]

    def best(key: str):
        vals = [f[key] for f in log if f.get(key) is not None]
        return max(vals) if vals else None

    return {"flights": len(log), "hours": round(sum(f["minutes"] for f in log) / 60, 1),
            "km": round(sum(f["flown_km"] for f in log), 1), "highest_m": best("max_alt_m"), "furthest_m": best("furthest_m"),
            "fastest_kmh": best("max_speed_kmh"), "places": len({f["place"] for f in log if f["place"]}),
            "year": year, "flights_this_year": len(this_year), "hours_this_year": round(sum(f["minutes"] for f in this_year) / 60, 1),
            "last": log[0] if log else None}


class Notes:
    """A note per flight, by its id (its start time), kept in the data folder."""

    def __init__(self, path: Path):
        self.path = path

    def all(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set(self, fid: str, text: str) -> None:
        data = self.all()
        if text.strip():
            data[fid] = text.strip()[:500]
        else:
            data.pop(fid, None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)


_cache: dict = {"at": 0.0, "key": None, "log": []}


def cached(lib, notes: Notes, seconds: float = 60.0) -> list[dict]:
    """The log, worked out at most once a minute (each clip's .SRT is parsed and cached anyway)."""
    key = (id(lib), len(lib.list()), notes.path.stat().st_mtime if notes.path.exists() else 0)
    if _cache["key"] != key or time.time() - _cache["at"] > seconds:
        _cache.update(at=time.time(), key=key, log=flights(lib, notes.all()))
    return _cache["log"]
