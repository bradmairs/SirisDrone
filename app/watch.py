"""Watch folder: new drone files dropped into a NAS folder are edited and exported automatically.

Each new file is added to the library (linked, never moved or changed), gets the automatic
edit (BRM Blue, stamp, level horizon, exposure; D-Cinelike look for 10-bit video), and is
exported with a recipe: photos → Instagram post (panoramas → carousel), videos → Apple TV 4K.
Files still being copied are left alone until their size stops changing.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path

from . import jobs, recipes
from .config import resolve_in_root
from .library import media_kind

DEFAULT_WATCH = {
    "enabled": False,
    "root": None,            # an input root (SIRIS_INPUT_ROOTS), e.g. "Drone footage"
    "folder": "",            # sub-folder inside it, e.g. "Inbox"
    "recursive": False,
    "out_root": None,        # an output root, e.g. "NAS"
    "out_folder": "",        # e.g. "Auto exports"
    "photos": "instagram",   # instagram | full | skip
    "videos": "appletv",     # appletv | full | skip
    "auto_level": True,
    "auto_exposure": True,
    "interval": 30,          # seconds between scans
    "process_existing": False,
}
PHOTO_ACTIONS = ("instagram", "full", "skip")
VIDEO_ACTIONS = ("appletv", "full", "skip")


def normalize_watch(w: dict | None) -> dict:
    o = {**DEFAULT_WATCH, **{k: v for k, v in (w or {}).items() if k in DEFAULT_WATCH}}
    for k in ("enabled", "recursive", "auto_level", "auto_exposure", "process_existing"):
        o[k] = bool(o[k])
    o["photos"] = o["photos"] if o["photos"] in PHOTO_ACTIONS else "instagram"
    o["videos"] = o["videos"] if o["videos"] in VIDEO_ACTIONS else "appletv"
    o["folder"] = str(o["folder"] or "").strip().strip("/\\")
    o["out_folder"] = str(o["out_folder"] or "").strip().strip("/\\")
    try:
        o["interval"] = int(min(max(float(o["interval"]), 10), 3600))
    except (TypeError, ValueError):
        o["interval"] = 30
    return o


class Watcher:
    def __init__(self, cfg, lib, get_settings, submit_export, export_dest):
        self.cfg, self.lib = cfg, lib
        self.get_settings = get_settings
        self.submit_export, self.export_dest = submit_export, export_dest
        self.state_file = cfg.data_dir / "watch_state.json"
        self.seen: dict = self._load()
        self.pending: dict[str, tuple] = {}     # path -> (size, mtime) at last scan
        self.min_age = 10.0                     # seconds a file must be untouched
        self.status = {"last_scan": None, "processed": 0, "errors": [], "recent": [], "waiting": 0}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- persistence ----------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.seen))
        tmp.replace(self.state_file)

    # -- thread ---------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="watch-folder")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            w = normalize_watch(self.get_settings().get("watch"))
            if w["enabled"]:
                try:
                    self.scan()
                except Exception as exc:  # keep watching after unexpected errors
                    self._error(f"Scan failed: {exc}")
                    traceback.print_exc()
            self._wake.wait(w["interval"])
            self._wake.clear()

    # -- scanning -------------------------------------------------------------------
    def folder(self, w: dict) -> Path:
        return resolve_in_root(self.cfg.input_roots, w["root"] or "", w["folder"])

    def _files(self, w: dict) -> list[Path]:
        base = self.folder(w)
        if not base.is_dir():
            raise FileNotFoundError(f"Watch folder {base} not found – is the NAS mounted?")
        it = base.rglob("*") if w["recursive"] else base.iterdir()
        return sorted(p for p in it if p.is_file() and not p.name.startswith(".") and media_kind(p.name))

    def baseline(self) -> int:
        """Mark everything currently in the folder as already handled."""
        w = normalize_watch(self.get_settings().get("watch"))
        n = 0
        with self._lock:
            for p in self._files(w):
                st = p.stat()
                if str(p) not in self.seen:
                    self.seen[str(p)] = {"size": st.st_size, "mtime": st.st_mtime, "item": None,
                                         "at": time.time(), "skipped": "already there when watching started"}
                    n += 1
            self._save()
        return n

    def scan(self) -> int:
        """One pass: process files that are new and finished copying. Returns how many."""
        w = normalize_watch(self.get_settings().get("watch"))
        done = 0
        with self._lock:
            self.status["last_scan"] = time.time()
            waiting = 0
            for p in self._files(w):
                key = str(p)
                st = p.stat()
                sig = (st.st_size, st.st_mtime)
                prev = self.seen.get(key)
                if prev and (prev["size"], prev["mtime"]) == sig:
                    continue
                # Only touch files that stopped changing since the last scan (copy finished).
                if self.pending.get(key) != sig or time.time() - st.st_mtime < self.min_age:
                    self.pending[key] = sig
                    waiting += 1
                    continue
                self.pending.pop(key, None)
                entry = {"size": st.st_size, "mtime": st.st_mtime, "item": None, "at": time.time()}
                try:
                    entry.update(self.process(p, w))
                    done += 1
                    self.status["processed"] += 1
                except Exception as exc:
                    entry["error"] = str(exc)
                    self._error(f"{p.name}: {exc}")
                self.seen[key] = entry
                self.status["recent"] = ([{"name": p.name, **entry}] + self.status["recent"])[:20]
                self._save()
            self.status["waiting"] = waiting
        return done

    def process(self, path: Path, w: dict) -> dict:
        kind = media_kind(path.name)
        action = w["photos"] if kind == "photo" else w["videos"]
        default_edit = self.get_settings()["default_edit"]
        item = self.lib.add_linked(path, default_edit)
        edit = self.lib.get_edit(item["id"])
        t = (item.get("duration") or 0) * 0.3
        full = {**edit, "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}
        src = self.lib.preview_source(item, full, 1200, t)
        edit = recipes.auto_edit(item, edit, src, w["auto_level"], w["auto_exposure"], path=path)
        if action == "skip":
            self.lib.set_edit(item["id"], edit)
            return {"item": item["id"], "action": "edited only"}
        recipe = recipes.resolve(action, item, edit)
        edit = self.lib.set_edit(item["id"], recipes.recipe_edit(recipe, item, edit))
        base = self.get_settings()["export"]
        opts = jobs.normalize_export({**base, "root": w["out_root"] or base["root"],
                                      "folder": w["out_folder"], **recipes.RECIPES[recipe]["options"]})
        dest = self.export_dest(opts)
        job = self.submit_export(item, edit, opts, dest, carousel=recipe == "carousel")
        return {"item": item["id"], "action": recipe, "job": job["id"]}

    def _error(self, msg: str) -> None:
        self.status["errors"] = ([{"at": time.time(), "message": msg}] + self.status["errors"])[:20]
