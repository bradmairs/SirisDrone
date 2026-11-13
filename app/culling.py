"""Smart culling: flag blurry shots and near-duplicates (bursts, repeats) so only the best of
each group is exported.

Per photo we measure edge width (how many pixels the strongest edges take to go from dark to
light – crisp detail is 1–2 px, camera shake or missed focus smears it), clipping, a 64-bit
difference hash (what the picture looks like) and the capture time. Edge width doesn't care
how much detail a scene has, so a calm sea with a crisp horizon still counts as sharp.
Within a trip:

* **duplicates** – shots taken within a few seconds of each other (or with neighbouring file
  numbers when there's no time) that look nearly identical form a group; the sharpest,
  least clipped one is kept and the rest are rejected as "duplicate of …".
* **blurry** – edges clearly smeared (``BLUR_WIDTH`` px or more), or more than twice as wide as
  the trip's typical shot.

Rejections are suggestions: an item's `cull_override` ("keep" / "reject") always wins.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import numpy as np
from PIL import Image

SIZE = 2048             # measured at the proxy size, so it's resolution-independent
DUP_HAMMING = 10        # of 64 hash bits: at or below this, two shots look the same
DUP_SECONDS = 8.0       # ...and were taken this close together
DUP_FILE_GAP = 3        # without times: neighbouring DJI file numbers count as "close"
BLUR_WIDTH = 8.0        # median edge width (px at SIZE) that is blurry on its own
BLUR_RATIO = 2.2        # ...or this many times the trip's typical edge width (and >= 4 px)
MIN_FOR_BLUR = 4        # need a few shots before "much softer than the rest" means anything
MIN_EDGES = 200         # fewer crisp-edge pixels than this means the whole frame is soft


def features(img: Image.Image, exif_bytes=None, name: str = "") -> dict:
    """Measurements for one upright photo (any size; a ~2048px proxy is plenty)."""
    g = img.convert("L")
    g.thumbnail((SIZE, SIZE), Image.BILINEAR)
    a = np.asarray(g, np.float32)
    clip = float(((a > 251) | (a < 3)).mean())
    return {"width": edge_width(a), "clip": round(clip, 4), "hash": dhash(img),
            "time": capture_time(exif_bytes), "num": file_number(name)}


def edge_width(a: np.ndarray, samples: int = 1500) -> float:
    """Median width (px) of the strongest 0.5% of edges, along rows and columns."""
    rng = np.random.default_rng(0)
    widths: list[int] = []
    for b in (a, a.T):
        grad = np.zeros_like(b)
        grad[:, 1:-1] = (b[:, 2:] - b[:, :-2]) / 2
        mag = np.abs(grad)
        ys, xs = np.nonzero(mag >= max(float(np.quantile(mag, 0.995)), 8.0))
        if len(xs) < MIN_EDGES:  # no crisp edges at all: measure the strongest soft ones instead
            ys, xs = np.nonzero(mag >= max(float(np.quantile(mag, 0.999)), 1.0))
        if not len(xs):
            continue
        pick = rng.choice(len(xs), min(samples, len(xs)), replace=False)
        last = b.shape[1] - 1
        for y, x in zip(ys[pick], xs[pick]):
            row, sign = b[y], np.sign(grad[y, x])
            left = right = x
            while left > 0 and (row[left] - row[left - 1]) * sign > 0.5 and x - left < 40:
                left -= 1
            while right < last and (row[right + 1] - row[right]) * sign > 0.5 and right - x < 40:
                right += 1
            widths.append(right - left)
    return float(np.median(widths)) if widths else 1.0


def dhash(img: Image.Image) -> str:
    """64-bit difference hash as 16 hex digits."""
    g = np.asarray(img.convert("L").resize((9, 8), Image.BILINEAR), np.int16)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def capture_time(exif_bytes) -> float | None:
    if not exif_bytes:
        return None
    try:
        ex = Image.Exif()
        ex.load(exif_bytes)
        raw = ex.get_ifd(0x8769).get(36867) or ex.get(306)
        t = dt.datetime.strptime(str(raw)[:19], "%Y:%m:%d %H:%M:%S")
        sub = ex.get_ifd(0x8769).get(37521)  # SubSecTimeOriginal: bursts share a second
        return t.timestamp() + (float(f"0.{sub}") if sub and str(sub).isdigit() else 0.0)
    except Exception:
        return None


def file_number(name: str) -> int | None:
    m = re.search(r"(\d{3,})(?=\D*$)", Path(name).stem)
    return int(m.group(1)) if m else None


def quality(f: dict) -> float:
    """Higher is better: crisp edges first, then little clipping."""
    return -f["width"] - 4 * f["clip"]


def _close(a: dict, b: dict) -> bool:
    if a.get("time") is not None and b.get("time") is not None:
        return abs(a["time"] - b["time"]) <= DUP_SECONDS
    if a.get("num") is not None and b.get("num") is not None:
        return abs(a["num"] - b["num"]) <= DUP_FILE_GAP
    return False


def decide(shots: list[tuple[str, str, dict]]) -> dict[str, dict]:
    """shots: (id, name, features) for one trip. Returns id -> {"status", "reason", "group"}."""
    order = sorted(shots, key=lambda s: (s[2].get("time") is None, s[2].get("time") or 0,
                                         s[2].get("num") or 0, s[1]))
    # Chain consecutive look-alikes taken close together into groups.
    groups: list[list[tuple[str, str, dict]]] = []
    for s in order:
        last = groups[-1][-1] if groups else None
        if last and _close(last[2], s[2]) and hamming(last[2]["hash"], s[2]["hash"]) <= DUP_HAMMING:
            groups[-1].append(s)
        else:
            groups.append([s])
    median = float(np.median([s[2]["width"] for s in shots])) if shots else 1.0
    out: dict[str, dict] = {}
    for gi, grp in enumerate(groups):
        best = max(grp, key=lambda s: quality(s[2]))
        for s in grp:
            gid = f"g{gi}" if len(grp) > 1 else None
            if s is not best:
                out[s[0]] = {"status": "reject", "reason": f"duplicate of {best[1]}", "group": gid}
            elif s[2]["width"] >= BLUR_WIDTH or (len(shots) >= MIN_FOR_BLUR and s[2]["width"]
                                                   >= max(4.0, BLUR_RATIO * median)):
                out[s[0]] = {"status": "reject", "reason": "blurry", "group": gid}
            else:
                out[s[0]] = {"status": "keep", "reason": "best of similar shots" if gid else "", "group": gid}
    return out


def effective(item: dict) -> str:
    """"keep" or "reject" for an item, honouring the user's override."""
    override = item.get("cull_override")
    if override in ("keep", "reject"):
        return override
    return (item.get("cull") or {}).get("status", "keep")


def cull_library(lib, items: list[dict]) -> dict[str, dict]:
    """Cull photos trip by trip. Each trip includes its other photos already in the library, so
    a new shot is compared with earlier ones. Stores each item's result as `cull` and returns
    {id: result} for the given items."""
    photos = [lib.ensure_features(i) for i in items if i["kind"] == "photo"]
    trips: dict[str, list[dict]] = {}
    for it in photos:
        trips.setdefault(lib.trip_key(it), []).append(it)
    wanted = {i["id"] for i in photos}
    results: dict[str, dict] = {}
    everything = None
    for key, members in trips.items():
        if everything is None:
            everything = lib.list()
        ids = {m["id"] for m in members}
        members = members + [lib.ensure_features(o) for o in everything
                             if o["kind"] == "photo" and o["id"] not in ids and lib.trip_key(o) == key]
        shots = [(m["id"], m["name"], m["cull_features"]) for m in members if m.get("cull_features")]
        for mid, res in decide(shots).items():
            lib.update_meta(mid, cull=res)
            if mid in wanted:
                results[mid] = res
    return results
