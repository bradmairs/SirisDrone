"""Media library: uploaded or NAS-linked photos/videos, their edits and preview caches."""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from . import culling, geo, render, video
from .edits import normalize_edit

PROXY_EDGE = 2048
THUMB_EDGE = 420
FRAME_EDGE = 3840


TYPE_FOLDERS = ("dng", "jpeg", "jpg", "raw", "videos", "video", "mp4", "photos")


def trip_folder(path: Path) -> str:
    """A file's trip: its folder, ignoring a DNG/, JPEG/ or Videos/ level."""
    parent = Path(path).parent
    while parent.name.lower() in TYPE_FOLDERS:
        parent = parent.parent
    return str(parent)


def media_kind(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    if ext in render.PHOTO_EXTS:
        return "photo"
    if ext in video.VIDEO_EXTS:
        return "video"
    return None


class _LRU:
    def __init__(self, size: int):
        self.size, self.data, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                return self.data[key]
        return None

    def put(self, key, value):
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)

    def drop(self, prefix):
        with self.lock:
            for k in [k for k in self.data if k[0] == prefix]:
                del self.data[k]


class Library:
    def __init__(self, media_dir: Path):
        self.dir = media_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._proxies = _LRU(12)
        self._full = _LRU(2)
        self._frames = _LRU(8)

    # -- items --------------------------------------------------------------
    def _item_dir(self, mid: str) -> Path:
        if not mid.isalnum():
            raise KeyError(mid)
        return self.dir / mid

    def get(self, mid: str) -> dict:
        p = self._item_dir(mid) / "meta.json"
        if not p.exists():
            raise KeyError(mid)
        return json.loads(p.read_text())

    def list(self) -> list[dict]:
        items = []
        for d in self.dir.iterdir():
            m = d / "meta.json"
            if m.exists():
                try:
                    items.append(json.loads(m.read_text()))
                except ValueError:
                    continue
        return sorted(items, key=lambda i: i.get("added", 0), reverse=True)

    def source_path(self, item: dict) -> Path:
        if item["source"] == "linked":
            return Path(item["path"])
        return self._item_dir(item["id"]) / item["path"]

    def _write_meta(self, item: dict) -> None:
        d = self._item_dir(item["id"])
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(item, indent=1))
        tmp.replace(d / "meta.json")

    def _register(self, mid: str, name: str, kind: str, source: str, path: str,
                  src: Path, default_edit: dict | None) -> dict:
        item = {"id": mid, "name": name, "kind": kind, "source": source, "path": path,
                "added": time.time(), "bytes": src.stat().st_size}
        try:
            if kind == "photo":
                item["width"], item["height"] = render.photo_size(src)
            else:
                item.update(video.probe(src))
        except Exception as exc:
            shutil.rmtree(self._item_dir(mid), ignore_errors=True)
            raise ValueError(f"{name}: unsupported or damaged file ({exc})") from exc
        self._write_meta(item)
        edit = self.set_edit(mid, default_edit or {})
        if not edit["location"]["text"]:
            text = self.detect_location(item)
            if text:
                self.set_edit(mid, {**edit, "location": {**edit["location"], "text": text}})
        try:
            self.make_thumb(item)
        except Exception:
            pass  # preview will surface the problem; keep the item
        if kind == "photo":
            item = self.ensure_features(item)
        return item

    def update_meta(self, mid: str, **fields) -> dict:
        """Change some of an item's stored fields (None removes one)."""
        with self._lock:
            item = self.get(mid)
            for k, v in fields.items():
                if v is None:
                    item.pop(k, None)
                else:
                    item[k] = v
            self._write_meta(item)
        return item

    def ensure_gps(self, item: dict) -> dict:
        """Where the shot was taken, (lat, lon) as item["gps"] ([] when unknown), stored once."""
        if "gps" in item:
            return item
        gps = None
        try:
            src = self.source_path(item)
            if item["kind"] == "photo":
                with Image.open(src) as im:
                    gps = geo.gps_from_exif(im.getexif().tobytes())
            else:
                gps = geo.gps_for_video(src, item)
        except Exception:
            gps = None
        try:
            return self.update_meta(item["id"], gps=[round(gps[0], 6), round(gps[1], 6)] if gps else [])
        except KeyError:
            return item

    def ensure_features(self, item: dict) -> dict:
        """Culling measurements for a photo (see culling.py), computed once and stored."""
        if item["kind"] != "photo" or item.get("cull_features"):
            return item
        try:
            exif = None
            with Image.open(self.source_path(item)) as im:
                exif = im.getexif().tobytes()
        except Exception:
            pass
        try:
            f = culling.features(self.proxy(item), exif, item["name"])
        except Exception:
            return item
        return self.update_meta(item["id"], cull_features=f)


    def trip_key(self, item: dict) -> str:
        """Which trip a shot belongs to, for culling: its NAS folder (ignoring a JPEG/, DNG/ or
        Videos/ level), or for uploads the day it was taken."""
        if item["source"] == "linked":
            return trip_folder(Path(item["path"]))
        t = (item.get("cull_features") or {}).get("time")
        return "upload:" + (time.strftime("%Y-%m-%d", time.localtime(t)) if t else "undated")

    def detect_location(self, item: dict) -> str:
        """"City, Country" from the photo's EXIF GPS or the video's GPS (metadata or DJI .SRT)."""
        try:
            src = self.source_path(item)
            if item["kind"] == "photo":
                with Image.open(src) as im:  # JPEG and DNG (TIFF container) both work
                    gps = geo.gps_from_exif(im.getexif().tobytes())
            else:
                gps = geo.gps_for_video(src, item)
            return geo.city_country(gps)
        except Exception:
            return ""

    def add_upload(self, filename: str, stream: BinaryIO, default_edit: dict | None = None) -> dict:
        name = Path(filename).name
        kind = media_kind(name)
        if not kind:
            raise ValueError(f"{name}: not a supported photo or video type")
        mid = uuid.uuid4().hex[:12]
        d = self._item_dir(mid)
        d.mkdir(parents=True)
        dest = d / ("original" + Path(name).suffix.lower())
        with open(dest, "wb") as fh:
            shutil.copyfileobj(stream, fh, length=8 * 1024 * 1024)
        return self._register(mid, name, kind, "upload", dest.name, dest, default_edit)

    def add_generated(self, name: str, ext: str, write, default_edit: dict | None = None) -> dict:
        """Add a file the app made itself (e.g. a merged HDR). `write(path)` creates it."""
        mid = uuid.uuid4().hex[:12]
        d = self._item_dir(mid)
        d.mkdir(parents=True)
        dest = d / ("original" + ext)
        write(dest)
        return self._register(mid, name, media_kind(name) or "photo", "upload", dest.name, dest, default_edit)

    def add_linked(self, path: Path, default_edit: dict | None = None) -> dict:
        kind = media_kind(path.name)
        if not kind or not path.is_file():
            raise ValueError(f"{path.name}: not a supported photo or video file")
        for it in self.list():
            if it["source"] == "linked" and it["path"] == str(path):
                return it
        mid = uuid.uuid4().hex[:12]
        self._item_dir(mid).mkdir(parents=True)
        return self._register(mid, path.name, kind, "linked", str(path), path, default_edit)

    def delete(self, mid: str) -> None:
        self.get(mid)
        shutil.rmtree(self._item_dir(mid))
        for c in (self._proxies, self._full, self._frames):
            c.drop(mid)

    # -- edits --------------------------------------------------------------
    def get_edit(self, mid: str) -> dict:
        p = self._item_dir(mid) / "edit.json"
        return normalize_edit(json.loads(p.read_text()) if p.exists() else {})

    def set_edit(self, mid: str, edit: dict) -> dict:
        edit = normalize_edit(edit)
        d = self._item_dir(mid)
        tmp = d / "edit.json.tmp"
        tmp.write_text(json.dumps(edit))
        tmp.replace(d / "edit.json")
        return edit

    # -- pixels -------------------------------------------------------------
    def thumb_path(self, mid: str) -> Path:
        return self._item_dir(mid) / "thumb.jpg"

    def make_thumb(self, item: dict) -> None:
        if item["kind"] == "photo":
            src = self.proxy(item)
        else:
            t = min(1.0, item.get("duration", 0) * 0.25)
            src = Image.fromarray(video.extract_frame(self.source_path(item), item, t, PROXY_EDGE))
        th = src.copy()
        th.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
        th.save(self.thumb_path(item["id"]), "JPEG", quality=85)

    def proxy(self, item: dict) -> Image.Image:
        """~2048px upright 8-bit version of a photo, cached on disk and in memory."""
        mid = item["id"]
        cached = self._proxies.get((mid, "proxy"))
        if cached is not None:
            return cached
        p = self._item_dir(mid) / "proxy.jpg"
        if p.exists():
            img = Image.open(p).convert("RGB")
        else:
            src, _ = render.load_photo(self.source_path(item), PROXY_EDGE)
            img = src if isinstance(src, Image.Image) else Image.fromarray(
                render.to_uint8(render.to_float(src)))
            img.save(p, "JPEG", quality=92)
        img.load()
        self._proxies.put((mid, "proxy"), img)
        return img

    def full(self, item: dict):
        """Full-resolution decoded photo (source, info); cached for the latest two."""
        key = (item["id"], "full")
        cached = self._full.get(key)
        if cached is None:
            cached = render.load_photo(self.source_path(item))
            self._full.put(key, cached)
        return cached

    def preview_source(self, item: dict, edit: dict, want: int, t: float = 0.0):
        """Pick the cheapest source that still has enough pixels for the preview."""
        c = edit["crop"]
        frac = max(c["w"], c["h"])
        need = want / max(frac, 0.05)
        long_src = max(item["width"], item["height"])
        if item["kind"] == "photo":
            if need > PROXY_EDGE * 1.1 and long_src > PROXY_EDGE * 1.1:
                return self.full(item)[0]
            return self.proxy(item)
        # Decoding dominates, so decode each frame once at up to 4K and reuse it for every zoom level.
        level = FRAME_EDGE if long_src > FRAME_EDGE else None
        key = (item["id"], round(t, 2))
        frame = self._frames.get(key)
        if frame is None:
            frame = Image.fromarray(video.extract_frame(self.source_path(item), item, t, level))
            self._frames.put(key, frame)
        return frame
