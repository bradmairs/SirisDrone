"""Watch folder: new drone files dropped into a NAS folder are edited and exported automatically.

Each new file is added to the library (linked, never moved or changed), gets the automatic
edit (BRM Blue, stamp, level horizon, exposure; D-Cinelike look for 10-bit video), and is
exported with a recipe: photos → full-resolution JPEG, videos → Apple TV 4K.
Files still being copied are left alone until their size stops changing.

Each scan's new files are exported as a batch, after two trip-wide passes:

* `cull` – photos are culled with the rest of their trip: blurry shots and lesser
  near-duplicates are kept in the library, marked rejected, and neither exported nor deleted.
  An item's keep/reject override in the library wins.
* `match` – exposure and white balance are evened out across the trip (see tripmatch.py), so
  photos posted together and clips cut together don't jump in brightness or colour.

With `reel` on, a trip's highlight reel (see reel.py) is made once its clips have all arrived:
on the first scan with nothing still copying and no new files from that trip. It goes into the
trip's export folder and is remade, replacing the old one, when more clips arrive later. With
`delete_originals` also on, the trip's clips are kept until the reel is done.

When a trip is finished (nothing still copying or exporting, its reel made) a notification
sums it up: what was exported, what was skipped as blurry/duplicate, the reel and any errors,
with a few thumbnails (see notify.py). A scan that fails, e.g. because the NAS isn't mounted,
is notified too, at most once an hour per problem.

With `delete_originals` on, a file picked up while it is on is deleted from the watch folder
(and dropped from the library) once its export has finished and the exported file exists. For
when the watch folder holds copies of originals kept elsewhere. Failed or cancelled exports,
"edit only" files, files that changed since, and anything outside the watch folder are kept.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path

from PIL import Image

from . import culling, jobs, notify, recipes, tripmatch
from .config import resolve_in_root
from .library import media_kind, trip_folder

DEFAULT_WATCH = {
    "enabled": False,
    "root": None,            # an input root (SIRIS_INPUT_ROOTS), e.g. "Drone footage"
    "folder": "",            # sub-folder inside it, e.g. "Inbox"
    "recursive": False,
    "out_root": None,        # an output root, e.g. "NAS"
    "out_folder": "",        # e.g. "Auto exports"
    "photos": "full",        # full | skip
    "videos": "appletv",     # appletv | full | skip
    "auto_level": True,
    "auto_exposure": True,
    "interval": 30,          # seconds between scans
    "process_existing": False,
    "delete_originals": False,  # delete each picked-up file once its export is done
    "trip_folders": True,     # export into a folder per trip (see export_subfolder)
    "cull": True,             # don't export blurry shots or near-duplicates (see culling.py)
    "match": True,            # even out exposure / white balance across a trip (tripmatch.py)
    "reel": False,            # make a highlight reel per trip (reel.py)
    "reel_length": 60,        # seconds
}
PHOTO_ACTIONS = ("full", "skip")
VIDEO_ACTIONS = ("appletv", "full", "skip")


def normalize_watch(w: dict | None) -> dict:
    o = {**DEFAULT_WATCH, **{k: v for k, v in (w or {}).items() if k in DEFAULT_WATCH}}
    for k in ("enabled", "recursive", "auto_level", "auto_exposure", "process_existing",
              "delete_originals", "trip_folders", "cull", "match", "reel"):
        o[k] = bool(o[k])
    o["photos"] = "skip" if o["photos"] == "skip" else "full"  # Instagram options were removed in 1.1
    o["videos"] = o["videos"] if o["videos"] in VIDEO_ACTIONS else "appletv"
    o["folder"] = str(o["folder"] or "").strip().strip("/\\")
    o["out_folder"] = str(o["out_folder"] or "").strip().strip("/\\")
    try:
        o["interval"] = int(min(max(float(o["interval"]), 10), 3600))
    except (TypeError, ValueError):
        o["interval"] = 30
    try:
        o["reel_length"] = int(min(max(float(o["reel_length"]), 15), 300))
    except (TypeError, ValueError):
        o["reel_length"] = 60
    return o


class Watcher:
    def __init__(self, cfg, lib, get_settings, submit_export, export_dest, get_job=lambda jid: None,
                 submit_reel=None):
        self.cfg, self.lib = cfg, lib
        self.get_job = get_job                  # job id -> public job dict, or None if unknown
        self.get_settings = get_settings
        self.submit_export, self.export_dest = submit_export, export_dest
        self.submit_reel = submit_reel
        self.state_file = cfg.data_dir / "watch_state.json"
        self.seen: dict = self._load()
        self.pending: dict[str, tuple] = {}     # path -> (size, mtime) at last scan
        self.min_age = 10.0                     # seconds a file must be untouched
        self.status = {"last_scan": None, "processed": 0, "errors": [], "recent": [], "waiting": 0,
                       "notifications": []}
        self.trips: dict[str, dict] = {}        # trip folder -> what happened to it, until notified
        self._alerted: dict[str, float] = {}    # problem -> when it was last notified
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
                    self._alert(f"Watch folder problem: {exc}")
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
            self.cleanup(w)
            waiting = 0
            deferred: list[tuple[str, Path]] = []
            touched: set[str] = set()
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
                touched.add(str(p.parent))
                try:
                    entry.update(self.process(p, w))
                    if entry.get("action") == "batch":
                        deferred.append((key, p))
                    else:
                        done += 1
                        self.status["processed"] += 1
                except Exception as exc:
                    entry["error"] = str(exc)
                    self._error(f"{p.name}: {exc}")
                self.seen[key] = entry
                if entry.get("action") != "batch":
                    self.status["recent"] = ([{"name": p.name, **entry}] + self.status["recent"])[:20]
                    self._track(p, entry)
                self._save()
            done += self._finish_batch(deferred, w)
            self.status["waiting"] = waiting
            if not waiting:
                self._queue_reels(w, touched)
                self._check_trips(touched)
        return done

    def _finish_batch(self, deferred: list[tuple[str, Path]], w: dict) -> int:
        """Cull this scan's photos and match colours across each trip, then export the keepers."""
        if not deferred:
            return 0
        items = []
        for key, _ in deferred:
            try:
                items.append(self.lib.get(self.seen[key]["item"]))
            except KeyError:
                pass
        if w["cull"]:
            try:
                culling.cull_library(self.lib, [i for i in items if i["kind"] == "photo"])
            except Exception as exc:  # culling must never stop exports
                self._error(f"Culling failed, exporting everything: {exc}")
        if w["match"]:
            try:
                keep = [self.lib.get(i["id"]) for i in items]
                keep = [i for i in keep if culling.effective(i) == "keep"]
                ids = {i["id"] for i in keep}
                trips = {self.lib.trip_key(i) for i in keep}
                others = [o for o in self.lib.list() if o["id"] not in ids and culling.effective(o) == "keep"
                          and self.lib.trip_key(o) in trips]
                tripmatch.match_library(self.lib, keep, reference=others)
            except Exception as exc:  # matching must never stop exports
                self._error(f"Colour matching failed, exporting as edited: {exc}")
        done = 0
        for key, path in deferred:
            entry = self.seen[key]
            try:
                item = self.lib.get(entry["item"])
                if culling.effective(item) == "reject":
                    reason = (item.get("cull") or {}).get("reason") or "rejected"
                    entry.update({"action": f"rejected: {reason}"})
                    entry.pop("delete_after", None)  # never delete a shot we didn't export
                else:
                    entry.update(self.export_item(item, self.lib.get_edit(item["id"]), path, w))
                done += 1
                self.status["processed"] += 1
            except Exception as exc:
                entry["error"] = str(exc)
                self._error(f"{path.name}: {exc}")
            self.status["recent"] = ([{"name": path.name, **entry}] + self.status["recent"])[:20]
            self._track(path, entry)
        self._save()
        return done

    # -- trip summaries and notifications --------------------------------------------
    def _trip_rec(self, trip: str) -> dict:
        return self.trips.setdefault(trip, {"photos": 0, "videos": 0, "rejected": 0, "errors": [],
                                            "jobs": {}, "images": [], "reel": None, "folder": ""})

    def _track(self, path: Path, entry: dict) -> None:
        """Note what happened to one file, for its trip's notification."""
        action = str(entry.get("action") or "")
        jids = [j for j in (entry.get("jobs") or [entry.get("job")]) if j]
        if entry.get("pair") or not (entry.get("error") or action.startswith("rejected") or jids):
            return  # DNG twins and "edit only" files aren't reported
        rec = self._trip_rec(trip_folder(path))
        if entry.get("error"):
            rec["errors"].append(f"{path.name}: {entry['error']}")
        elif action.startswith("rejected"):
            rec["rejected"] += 1
        for jid in jids:
            rec["jobs"][jid] = (path.name, media_kind(path.name))
        if jids and entry.get("item") and len(rec["images"]) < 6:
            try:
                rec["images"].append((f"{path.stem}.jpg", self.lib.thumb_path(entry["item"]).read_bytes()))
            except OSError:
                pass

    def _check_trips(self, touched: set[str]) -> None:
        """Notify each trip that is finished: nothing new this scan, no reel pending, every
        export done."""
        for trip in list(self.trips):
            if any(Path(t).is_relative_to(trip) for t in touched):
                continue
            if any(e.get("wait_reel") == trip for e in self.seen.values()):
                continue
            rec = self.trips[trip]
            found = {jid: self.get_job(jid) for jid in rec["jobs"]}
            if any(j and j["status"] in ("queued", "running") for j in found.values()):
                continue
            for jid, (name, kind) in rec["jobs"].items():
                j = found[jid]
                if not j or j["status"] != "done":
                    status = j["status"] if j else "lost (the app restarted)"
                    rec["errors"].append(f"{name}: export {status}" + (f": {j['error']}" if j and j.get("error") else ""))
                elif kind == "reel":
                    rec["reel"] = Path(j["output"]).name
                else:
                    rec["photos" if kind == "photo" else "videos"] += 1
                    rec["folder"] = rec["folder"] or str(Path(j["output"]).parent)
            del self.trips[trip]
            title, message, failed = notify.trip_message(Path(trip).name, rec)
            self._send(title, message, rec["images"], failed,
                       {k: rec[k] for k in ("photos", "videos", "rejected", "errors", "reel", "folder")})

    def _alert(self, message: str) -> None:
        """Notify a problem, at most once an hour for the same message."""
        now = time.time()
        if now - self._alerted.get(message, 0) < 3600:
            return
        self._alerted[message] = now
        self._send("SirisDrone needs attention", message, [], True, {"problem": message})

    def _send(self, title: str, message: str, images: list, failed: bool, summary: dict) -> None:
        n = notify.normalize_notify(self.get_settings().get("notify"))
        if not n["enabled"] or not notify.channels(n):
            return
        results = notify.send(n, title, message, images, summary, failed)
        for ch, res in results.items():
            if res != "sent":
                self._error(f"Notification by {ch} failed: {res}")
        self.status["notifications"] = ([{"at": time.time(), "title": title, "results": results}]
                                         + self.status["notifications"])[:10]

    @staticmethod
    def jpeg_twin(path: Path) -> Path | None:
        """The camera JPEG of the same shot when `path` is a DNG beside it (DJI saves both)."""
        if path.suffix.lower() != ".dng":
            return None
        return next((path.with_suffix(e) for e in (".JPG", ".jpg", ".JPEG", ".jpeg")
                     if path.with_suffix(e).exists()), None)

    def process(self, path: Path, w: dict) -> dict:
        twin = self.jpeg_twin(path)
        if twin is not None:
            # One edit per shot. The camera JPEG looks better than our DNG development (lens
            # corrections, sharpening, its own colour), so it is the one used.
            out = {"item": None, "action": "skipped: JPEG used", "pair": str(twin)}
            if w["delete_originals"]:
                out["delete_after"] = True
            return out
        kind = media_kind(path.name)
        action = w["photos"] if kind == "photo" else w["videos"]
        default_edit = self.get_settings()["default_edit"]
        item = self.lib.add_linked(path, default_edit)
        edit = self.lib.get_edit(item["id"])
        t = (item.get("duration") or 0) * 0.3
        full = {**edit, "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}
        src = self.lib.preview_source(item, full, 1200, t)
        edit = recipes.auto_edit(item, edit, src, w["auto_level"], w["auto_exposure"], path=path)
        out = {"item": item["id"]}
        if kind == "video" and w["reel"] and self.submit_reel is not None:
            out.update({"wait_reel": self.lib.trip_key(item), "reel_folder": self.out_folder(path, w, item)})
        if action == "skip":
            self.lib.set_edit(item["id"], edit, "Automatic edit")
            return {**out, "action": "edited only"}
        edit = self.lib.set_edit(item["id"], recipes.recipe_edit(action, item, edit), "Automatic edit")
        if (kind == "photo" and w["cull"]) or w["match"]:
            out["action"] = "batch"   # exported by _finish_batch at the end of the scan
            if w["delete_originals"]:
                out["delete_after"] = True
            return out
        return {**out, **self.export_item(item, edit, path, w)}

    def out_folder(self, path: Path, w: dict, item: dict) -> str:
        folder = w["out_folder"]
        if w["trip_folders"]:
            folder = "/".join(p for p in (folder, self.export_subfolder(path, w, item)) if p)
        return folder

    def _queue_reels(self, w: dict, touched: set[str]) -> None:
        """Make the highlight reel of each trip whose clips have all arrived (see module doc)."""
        due: dict[str, list[dict]] = {}
        for key, entry in self.seen.items():
            if entry.get("wait_reel"):
                due.setdefault(entry["wait_reel"], []).append(entry)
        for trip, entries in due.items():
            if any(Path(t).is_relative_to(trip) for t in touched):
                continue  # new files from this trip in this scan: wait for a quiet scan
            try:
                if not w["reel"] or self.submit_reel is None:
                    continue  # turned off since: just let the originals go
                clips = [i for i in self.lib.list() if i["kind"] == "video" and self.lib.trip_key(i) == trip
                         and culling.effective(i) == "keep"]
                if not clips:
                    continue
                recipe = w["videos"] if w["videos"] != "skip" else "appletv"
                base = self.get_settings()["export"]
                opts = jobs.normalize_export({**base, "root": w["out_root"] or base["root"],
                                              "folder": entries[0].get("reel_folder", w["out_folder"]),
                                              **recipes.RECIPES[recipe]["options"]})
                job = self.submit_reel(clips, opts, self.export_dest(opts), w["reel_length"], overwrite=True)
                self._trip_rec(trip)["jobs"][job["id"]] = (f"{Path(trip).name} highlights", "reel")
                for e in entries:
                    e["jobs"] = [j for j in (e.get("jobs") or [e.get("job")]) if j] + [job["id"]]
                self.status["recent"] = ([{"name": f"{Path(trip).name} highlights", "action": "highlight reel",
                                           "at": time.time()}] + self.status["recent"])[:20]
            except Exception as exc:
                self._error(f"Highlight reel for {Path(trip).name}: {exc}")
            finally:
                for e in entries:
                    e.pop("wait_reel", None)
        if due:
            self._save()

    def export_item(self, item: dict, edit: dict, path: Path, w: dict) -> dict:
        recipe = w["photos"] if item["kind"] == "photo" else w["videos"]
        base = self.get_settings()["export"]
        folder = self.out_folder(path, w, item)
        opts = jobs.normalize_export({**base, "root": w["out_root"] or base["root"],
                                      "folder": folder, **recipes.RECIPES[recipe]["options"]})
        dest = self.export_dest(opts)
        job = self.submit_export(item, edit, opts, dest)
        out = {"item": item["id"], "action": recipe, "job": job["id"], "jobs": [job["id"]]}
        if w["delete_originals"]:
            out["delete_after"] = True
        return out

    _TYPE_FOLDERS = ("dng", "jpeg", "jpg", "raw", "videos", "video", "mp4", "photos")

    def export_subfolder(self, path: Path, w: dict, item: dict) -> str:
        """Folder (inside the export folder) for one trip: the file's folder inside the watch
        folder, e.g. "2026-04-03_Philip Island - Australia" (a DNG/, JPEG/ or Videos/ level is
        left out so a trip's photos and videos end up together). Files dropped straight into
        the watch folder go into a folder named by the day they were taken, e.g. "2026-04-03"."""
        try:
            parts = list(path.parent.relative_to(self.folder(w)).parts)
        except ValueError:
            parts = []
        while parts and parts[-1].lower() in self._TYPE_FOLDERS:
            parts.pop()
        if parts:
            return "/".join(parts)
        if item["kind"] == "video":
            return jobs.capture_date(creation_time=item.get("creation_time"))
        try:
            with Image.open(path) as im:
                return jobs.capture_date(im.getexif().tobytes())
        except Exception:
            return jobs.capture_date()

    def cleanup(self, w: dict) -> int:
        """Delete picked-up files whose export finished (see module doc). Returns how many."""
        base = self.folder(w).resolve()
        removed = 0
        for key, entry in self.seen.items():
            if not entry.get("delete_after") or "deleted" in entry or "kept" in entry:
                continue
            if entry.get("wait_reel"):
                continue  # its trip's highlight reel isn't made yet
            path = Path(key)
            if not path.resolve().is_relative_to(base):
                continue  # watch folder changed since: leave it alone
            if "pair" in entry:  # a DNG skipped for its JPEG: goes once the JPEG has gone
                twin = self.seen.get(entry["pair"], {})
                if "deleted" in twin:
                    try:
                        st = path.stat()
                        if (st.st_size, st.st_mtime) == (entry["size"], entry["mtime"]):
                            path.unlink()
                            entry["deleted"] = time.time()
                            removed += 1
                        else:
                            entry["kept"] = "file changed after it was picked up"
                    except FileNotFoundError:
                        entry["deleted"] = time.time()
                    except OSError as exc:
                        entry["kept"] = f"couldn't delete: {exc.strerror or exc}"
                elif "kept" in twin:
                    entry["kept"] = "its JPEG was kept"
                if "kept" in entry:
                    self._error(f"{path.name}: original kept ({entry['kept']})")
                continue
            found = [self.get_job(j) for j in entry.get("jobs") or [entry.get("job")]]
            bad = next((j for j in found if j and j["status"] not in ("queued", "running", "done")), None)
            if None in found:
                entry["kept"] = "export didn't finish before the app restarted"
            elif any(j["status"] in ("queued", "running") for j in found):
                continue
            elif bad or not all(j.get("output") and Path(j["output"]).exists() for j in found):
                job = bad or found[0]
                entry["kept"] = f"export {job['status']}" + (f": {job['error']}" if job.get("error") else "")
            else:
                try:
                    st = path.stat()
                    if (st.st_size, st.st_mtime) != (entry["size"], entry["mtime"]):
                        entry["kept"] = "file changed after it was picked up"
                    else:
                        path.unlink()
                        entry["deleted"] = time.time()
                        removed += 1
                        try:
                            self.lib.delete(entry["item"])
                        except (KeyError, OSError):
                            pass
                except FileNotFoundError:
                    entry["deleted"] = time.time()  # already gone
                except OSError as exc:
                    entry["kept"] = f"couldn't delete: {exc.strerror or exc}"
            if "kept" in entry:
                self._error(f"{path.name}: original kept ({entry['kept']})")
        if removed or any("kept" in e or "deleted" in e for e in self.seen.values()):
            self._save()
        return removed

    def _error(self, msg: str) -> None:
        self.status["errors"] = ([{"at": time.time(), "message": msg}] + self.status["errors"])[:20]
