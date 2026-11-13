"""SirisDrone Studio – web app for editing drone photos & videos and exporting them to a NAS."""
from __future__ import annotations

import base64
import json
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analysis, culling, geo, gpu, grading, jobs, notify, recipes, reel, render, telemetry, tripmatch, video
from . import __version__
from .playback import Playback
from .config import DEFAULT_STAMP, STATIC_DIR, Config, resolve_in_root
from .edits import DEFAULT_EDIT, normalize_edit
from .library import Library, media_kind
from .stamp import load_mask
from .watch import Watcher, normalize_watch

MAX_PREVIEW = 2400


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config()
    cfg.ensure_dirs()
    tempfile.tempdir = str(cfg.tmp_dir)
    geo.configure(cfg.data_dir / "geocache.json")
    lib = Library(cfg.media_dir)
    queue = jobs.JobQueue()
    playback = Playback()
    app = FastAPI(title="SirisDrone Studio", version=__version__, docs_url="/api/docs")
    started_at = time.time()
    app.state.cfg, app.state.lib, app.state.jobs = cfg, lib, queue
    app.state.playback = playback

    def lut_path(lut_id: str) -> Path | None:
        return cfg.lut_dir / f"{lut_id}.cube" if lut_id.isalnum() else None

    grading.set_lut_loader(lut_path)

    # -- settings -------------------------------------------------------------
    def load_settings() -> dict:
        s = {}
        if cfg.settings_file.exists():
            try:
                s = json.loads(cfg.settings_file.read_text())
            except ValueError:
                s = {}
        export = jobs.normalize_export(s.get("export"))
        if export["root"] not in cfg.output_roots:
            export["root"] = next(iter(cfg.output_roots))
        presets = {str(k)[:60]: grading.normalize_grade(v)
                   for k, v in (s.get("presets") or {}).items() if isinstance(v, dict)}
        return {"export": export, "default_edit": normalize_edit(s.get("default_edit")),
                "presets": presets, "watch": normalize_watch(s.get("watch")),
                "notify": notify.normalize_notify(s.get("notify"))}

    def save_settings(s: dict) -> dict:
        cur = load_settings()
        if "export" in s:
            cur["export"] = jobs.normalize_export({**cur["export"], **s["export"]})
        if "default_edit" in s:
            cur["default_edit"] = normalize_edit(s["default_edit"])
        if "presets" in s and isinstance(s["presets"], dict):
            cur["presets"] = s["presets"]
        if "watch" in s:
            cur["watch"] = normalize_watch({**cur["watch"], **(s["watch"] or {})})
        if "notify" in s:
            cur["notify"] = notify.normalize_notify({**cur["notify"], **(s["notify"] or {})})
        cfg.settings_file.write_text(json.dumps(cur, indent=1))
        return load_settings()

    def stamp_file() -> Path:
        return cfg.custom_stamp if cfg.custom_stamp.exists() else DEFAULT_STAMP

    def stamp_mask():
        return load_mask(stamp_file())

    def item_or_404(mid: str) -> dict:
        try:
            return lib.get(mid)
        except KeyError:
            raise HTTPException(404, "Media not found")

    # -- optional basic auth -------------------------------------------------
    if cfg.user and cfg.password:
        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(user, cfg.user) and secrets.compare_digest(pw, cfg.password)
                except Exception:
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="SirisDrone"'})
            return await call_next(request)

    # -- app config ------------------------------------------------------------
    @app.get("/api/config")
    def get_config():
        roots = []
        for name, path in cfg.output_roots.items():
            roots.append({"name": name, "path": str(path), "available": path.is_dir()})
        return {
            "output_roots": roots,
            "input_roots": [{"name": n, "path": str(p), "available": p.is_dir()}
                            for n, p in cfg.input_roots.items()],
            "presets": grading.PRESETS,
            "default_edit": DEFAULT_EDIT,
            "neutral_grade": grading.DEFAULT_GRADE,
            "codecs": {k: v[0] for k, v in video.CODECS.items()},
            "version": __version__,
            "recipes": {k: {kk: r[kk] for kk in ("label", "kinds", "help")} for k, r in recipes.RECIPES.items()},
            "gpu": gpu.detect(),
            "photo_formats": list(render.PHOTO_FORMATS),
            "settings": load_settings(),
            "custom_stamp": cfg.custom_stamp.exists(),
        }

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        return save_settings(body)

    # -- media -----------------------------------------------------------------
    @app.get("/api/media")
    def list_media():
        return lib.list()

    @app.post("/api/media/upload")
    def upload(files: list[UploadFile] = File(...)):
        default_edit = load_settings()["default_edit"]
        added, errors, flight = [], [], []
        is_srt = lambda f: (f.filename or "").lower().endswith(".srt")
        for f in sorted(files, key=is_srt):  # clips before their .SRT flight logs
            try:
                if is_srt(f):
                    flight.append(lib.attach_srt(f.filename, f.file)["id"])
                else:
                    added.append(lib.add_upload(f.filename or "upload", f.file, default_edit))
            except ValueError as exc:
                errors.append(str(exc))
        return {"added": added, "errors": errors, "flight": flight}

    @app.post("/api/media/import")
    def import_media(body: dict = Body(...)):
        try:
            target = resolve_in_root(cfg.input_roots, body.get("root", ""), body.get("path", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not target.exists():
            raise HTTPException(404, "Not found – is the NAS share mounted?")
        default_edit = load_settings()["default_edit"]
        names = body.get("files")
        if target.is_dir():
            paths = [target / n for n in names] if names else sorted(target.iterdir())
        else:
            paths = [target]
        added, errors = [], []
        for p in paths:
            if p.parent.resolve() != (target if target.is_dir() else target.parent) or not media_kind(p.name):
                continue
            try:
                added.append(lib.add_linked(p, default_edit))
            except ValueError as exc:
                errors.append(str(exc))
        return {"added": added, "errors": errors}

    @app.delete("/api/media/{mid}")
    def delete_media(mid: str):
        item_or_404(mid)
        lib.delete(mid)
        return {"ok": True}

    @app.get("/api/media/{mid}/thumb")
    def thumb(mid: str):
        item = item_or_404(mid)
        p = lib.thumb_path(mid)
        if not p.exists():
            try:
                lib.make_thumb(item)
            except Exception:
                raise HTTPException(500, "Could not create thumbnail")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/media/{mid}/edit")
    def get_edit(mid: str):
        item_or_404(mid)
        return lib.get_edit(mid)

    @app.put("/api/media/{mid}/edit")
    def put_edit(mid: str, edit: dict = Body(...)):
        item_or_404(mid)
        return lib.set_edit(mid, edit)

    @app.get("/api/media/{mid}/flight")
    def flight_data(mid: str):
        """The clip's DJI telemetry summary and twice-a-second samples (for the editor)."""
        item = item_or_404(mid)
        rows = lib.flight(item)
        step, out, last = 0.5, [], -1e9
        for r in rows:
            if r["t"] - last >= step:
                out.append({k: r[k] for k in ("t", "alt", "speed", "dist", "lat", "lon") if k in r})
                last = r["t"]
        return {"available": bool(rows), "summary": telemetry.summary(rows) if rows else None,
                "samples": out}

    @app.get("/api/media/{mid}/history")
    def edit_history(mid: str):
        """Earlier versions of the edit, newest first (index counts from the oldest)."""
        item_or_404(mid)
        hist = lib.history(mid)
        return [{"index": i, "at": h["at"], "label": h["label"]} for i, h in reversed(list(enumerate(hist)))]

    @app.post("/api/media/{mid}/history/{index}/restore")
    def restore_edit(mid: str, index: int):
        item_or_404(mid)
        hist = lib.history(mid)
        if not 0 <= index < len(hist):
            raise HTTPException(404, "No such version")
        h = hist[index]
        when = time.strftime("%d %b %H:%M", time.localtime(h["at"]))
        return lib.set_edit(mid, h["edit"], f"Restored “{h['label']}” from {when}")

    @app.post("/api/media/{mid}/preview")
    def preview(mid: str, body: dict = Body(...)):
        """Render a JPEG preview. mode: "final" (everything) | "frame" (uncropped, for the crop tool)."""
        item = item_or_404(mid)
        edit = normalize_edit(body.get("edit") if body.get("edit") is not None else lib.get_edit(mid))
        size = int(min(max(int(body.get("size", 1400)), 200), MAX_PREVIEW))
        mode = body.get("mode", "final")
        t = float(body.get("t", 0) or 0)
        if body.get("original"):
            edit = normalize_edit({"grade": {}, "rotate": edit["rotate"], "flip_h": edit["flip_h"],
                                   "flip_v": edit["flip_v"], "straighten": edit["straighten"],
                                   "crop": edit["crop"], "stamp": {"enabled": False}})
        if item["kind"] == "video":
            edit["noise"] = 0  # photo-only: videos use their own noise reduction (FFmpeg)
        try:
            src = lib.preview_source(item, edit if mode != "frame" else
                                     {**edit, "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}, size, t)
            rgb = render.render(src, edit, long_edge=size, mode=mode, fast=True,
                                stamp_mask=stamp_mask(), allow_upscale=False)
            if mode == "final" and edit["flight"]["enabled"] and not body.get("original"):
                rows = lib.flight(item)
                if rows:
                    jobs.add_flight_overlay(rgb, rows, edit["flight"], t)
        except Exception as exc:
            raise HTTPException(500, f"Preview failed: {exc}")
        h, w = rgb.shape[:2]
        return Response(render.encode_jpeg(rgb, 88), media_type="image/jpeg",
                        headers={"X-Width": str(w), "X-Height": str(h), "Cache-Control": "no-store"})

    # -- video playback -------------------------------------------------------------
    def playback_public(state: dict) -> dict:
        out = {k: v for k, v in state.items() if k != "path"}
        if state["status"] == "ready":
            out["url"] = f"/api/playback/{state['key']}/file"
        return out

    @app.post("/api/media/{mid}/playback")
    def request_playback(mid: str, body: dict = Body(...)):
        """mode "original" plays the clip as shot; "edited" renders a quick preview of the edit."""
        item = item_or_404(mid)
        if item["kind"] != "video":
            raise HTTPException(400, "Only videos can be played")
        mode = "edited" if body.get("mode") == "edited" else "original"
        edit = body.get("edit") if body.get("edit") is not None else lib.get_edit(mid)
        sf = stamp_file()
        state = playback.request(item, lib.source_path(item), cfg.media_dir / mid, mode, edit,
                                 stamp_mask(), f"{sf}:{sf.stat().st_mtime}")
        return playback_public(state)

    @app.get("/api/playback/{key}")
    def playback_status(key: str):
        state = playback.get(key)
        if not state:
            raise HTTPException(404, "Unknown playback")
        return playback_public(state)

    @app.get("/api/playback/{key}/file")
    def playback_file(key: str):
        state = playback.get(key)
        if not state or state["status"] != "ready" or not Path(state["path"]).exists():
            raise HTTPException(404, "Not ready")
        return FileResponse(state["path"], media_type="video/mp4", headers={"Cache-Control": "no-cache"})

    # -- LUTs --------------------------------------------------------------------
    def list_luts():
        out = []
        for p in sorted(cfg.lut_dir.glob("*.cube")):
            meta = p.with_suffix(".json")
            name = json.loads(meta.read_text())["name"] if meta.exists() else p.stem
            out.append({"id": p.stem, "name": name})
        return sorted(out, key=lambda x: x["name"].lower())

    @app.get("/api/luts")
    def get_luts():
        return list_luts()

    @app.post("/api/luts")
    def upload_lut(file: UploadFile = File(...)):
        lid = uuid.uuid4().hex[:12]
        dest = cfg.lut_dir / f"{lid}.cube"
        with open(dest, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
        try:
            grading.read_cube(dest)
        except (ValueError, OSError) as exc:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, f"Invalid LUT: {exc}")
        name = Path(file.filename or "LUT").stem
        dest.with_suffix(".json").write_text(json.dumps({"name": name}))
        return {"id": lid, "name": name}

    @app.delete("/api/luts/{lid}")
    def delete_lut(lid: str):
        p = lut_path(lid)
        if not p or not p.exists():
            raise HTTPException(404, "LUT not found")
        p.unlink()
        p.with_suffix(".json").unlink(missing_ok=True)
        return {"ok": True}

    # -- stamp -------------------------------------------------------------------
    @app.get("/api/stamp")
    def get_stamp():
        return FileResponse(stamp_file(), headers={"Cache-Control": "no-store"})

    @app.post("/api/stamp")
    def upload_stamp(file: UploadFile = File(...)):
        from PIL import Image
        try:
            img = Image.open(file.file)
            img.load()
        except Exception:
            raise HTTPException(400, "Please upload a PNG (ideally transparent) or JPEG logo")
        img.save(cfg.custom_stamp, "PNG")
        return {"ok": True}

    @app.delete("/api/stamp")
    def reset_stamp():
        cfg.custom_stamp.unlink(missing_ok=True)
        return {"ok": True}

    # -- folders -------------------------------------------------------------------
    @app.get("/api/browse")
    def browse(kind: str = "output", root: str = "", path: str = ""):
        roots = cfg.output_roots if kind == "output" else cfg.input_roots
        try:
            target = resolve_in_root(roots, root, path)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not target.is_dir():
            raise HTTPException(404, "Folder not found – is the NAS share mounted?")
        dirs, files = [], []
        for p in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if p.name.startswith("."):
                continue
            if p.is_dir():
                dirs.append(p.name)
            elif kind == "input" and media_kind(p.name):
                files.append({"name": p.name, "kind": media_kind(p.name), "bytes": p.stat().st_size})
        return {"root": root, "path": path.strip("/"), "dirs": dirs, "files": files}

    @app.post("/api/mkdir")
    def mkdir(body: dict = Body(...)):
        root = body.get("root", "")
        if root in cfg.output_roots and not cfg.output_roots[root].is_dir():
            raise HTTPException(400, "Export folder is not reachable – is the NAS mounted?")
        try:
            target = resolve_in_root(cfg.output_roots, root, body.get("path", ""))
            target.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    # -- export ------------------------------------------------------------------
    def export_dest(opts: dict) -> Path:
        if opts["root"] not in cfg.output_roots:
            raise HTTPException(400, "Choose an export folder")
        if not cfg.output_roots[opts["root"]].is_dir():
            raise HTTPException(400, f"Export folder '{opts['root']}' is not reachable – is the NAS mounted?")
        try:
            dest = resolve_in_root(cfg.output_roots, opts["root"], opts["folder"])
            dest.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, f"Cannot use export folder: {exc}")
        return dest

    def submit_export(item: dict, edit: dict, opts: dict, dest: Path, n: int = 1,
                      frame_time: float | None = None) -> dict:
        mask = stamp_mask()
        src = lib.source_path(item)
        if frame_time is not None and item["kind"] == "video":
            kind = "frame"
            run = lambda job: jobs.export_photo(src, item["name"], edit, opts, dest, mask, job,
                                                frame=(item, float(frame_time)), n=n,
                                                flight=lib.flight(item))
        elif item["kind"] == "video":
            kind = "video"
            run = lambda job: jobs.export_video(src, item, item["name"], edit, opts, dest, mask, job, n=n,
                                                flight=lib.flight(item))
        else:
            kind = "photo"
            run = lambda job: jobs.export_photo(src, item["name"], edit, opts, dest, mask, job,
                                                n=n)
        return queue.submit(item["id"], item["name"], kind, run).public()

    app.state.submit_export, app.state.export_dest = submit_export, export_dest

    @app.post("/api/export")
    def export(body: dict = Body(...)):
        opts = jobs.normalize_export(body.get("options"))
        dest = export_dest(opts)
        save_settings({"export": opts})
        frame_time = body.get("frame_time")
        created = [submit_export(item_or_404(mid), lib.get_edit(mid), opts, dest, n, frame_time)
                   for n, mid in enumerate(body.get("media_ids", []), start=1)]
        return {"jobs": created, "destination": str(dest)}

    @app.post("/api/quick-export")
    def quick_export(body: dict = Body(...)):
        """One-click recipe export (Apple TV 4K, full resolution)."""
        name = body.get("recipe")
        if name not in recipes.RECIPES:
            raise HTTPException(400, "Unknown export recipe")
        base = load_settings()["export"]
        created, skipped = [], []
        dest = None
        for n, mid in enumerate(body.get("media_ids", []), start=1):
            item = item_or_404(mid)
            edit = lib.get_edit(mid)
            recipe = name
            if item["kind"] not in recipes.RECIPES[recipe]["kinds"]:
                skipped.append(item["name"])
                continue
            opts = jobs.normalize_export({**base, "root": body.get("root") or base["root"],
                                          "folder": body.get("folder", base["folder"]),
                                          **recipes.RECIPES[recipe]["options"]})
            dest = dest or export_dest(opts)
            edit = lib.set_edit(mid, recipes.recipe_edit(recipe, item, edit))  # show what was exported
            created.append(submit_export(item, edit, opts, dest, n))
        return {"jobs": created, "skipped": skipped, "destination": str(dest) if dest else None}

    def photo_items(ids: list, minimum: int, what: str) -> list[dict]:
        items = [item_or_404(m) for m in ids]
        items = [i for i in items if i["kind"] == "photo"]
        if len(items) < minimum:
            raise HTTPException(400, f"Select at least {minimum} photos for {what}")
        return items

    @app.post("/api/hdr-merge")
    def hdr_merge(body: dict = Body(...)):
        """Fuse selected exposure brackets into a new 16-bit library item."""
        items = photo_items(body.get("media_ids", []), 2, "a bracket merge")
        if len(items) > 9:
            raise HTTPException(400, "Select one bracket set (2–9 photos) at a time")
        paths = [lib.source_path(i) for i in items]
        names = [i["name"] for i in items]
        default_edit = load_settings()["default_edit"]
        add = lambda name, ext, write: lib.add_generated(name, ext, write, default_edit)
        job = queue.submit(items[0]["id"], f"HDR merge of {len(items)} photos", "hdr",
                           lambda job: jobs.merge_brackets(paths, names, add, job))
        return {"jobs": [job.public()]}

    @app.post("/api/hyperlapse")
    def hyperlapse(body: dict = Body(...)):
        """Make a video from a photo sequence, using one item's edit for every frame."""
        items = photo_items(body.get("media_ids", []), 4, "a hyperlapse")
        lead = item_or_404(body.get("edit_from") or items[0]["id"])
        recipe = recipes.RECIPES[body.get("recipe") or "appletv"]["options"]
        base = load_settings()["export"]
        opts = jobs.normalize_export({**base, **{k: v for k, v in recipe.items() if k.startswith("video")},
                                      "name_pattern": "{date} {place} {name}",
                                      "root": body.get("root") or base["root"],
                                      "folder": body.get("folder", base["folder"])})
        dest = export_dest(opts)
        fps = float(min(max(float(body.get("fps") or 25), 1), 60))
        paths = [lib.source_path(i) for i in items]
        names = [i["name"] for i in items]
        edit = lib.get_edit(lead["id"])
        mask = stamp_mask()
        job = queue.submit(lead["id"], f"Hyperlapse of {len(items)} photos", "hyperlapse",
                           lambda job: jobs.export_hyperlapse(paths, names, edit, opts, dest, mask, job, fps,
                                                              bool(body.get("stabilize", True)),
                                                              bool(body.get("deflicker", True))))
        return {"jobs": [job.public()], "destination": str(dest)}

    # -- trip highlight reel and colour match ------------------------------------------
    def submit_reel(items: list[dict], opts: dict, dest: Path, length: float = 60, seg: float = 4.0,
                    title: str = "", stem: str | None = None, overwrite: bool = False) -> dict:
        clips = [{"path": lib.source_path(i), "meta": i, "name": i["name"], "edit": lib.get_edit(i["id"])}
                 for i in items]
        mask = stamp_mask()
        return queue.submit(items[0]["id"], f"Highlight reel of {len(items)} clips", "reel",
                            lambda job: reel.make_reel(clips, opts, dest, mask, job, length, seg, title,
                                                       stem, overwrite)).public()

    app.state.submit_reel = submit_reel

    @app.post("/api/reel")
    def highlight_reel(body: dict = Body(...)):
        """Cut the best seconds of the selected clips into one video with a title card."""
        items = [item_or_404(m) for m in body.get("media_ids", [])]
        items = [i for i in items if i["kind"] == "video"]
        if not items:
            raise HTTPException(400, "Select the video clips of a trip")
        recipe = recipes.RECIPES[body.get("recipe") or "appletv"]["options"]
        base = load_settings()["export"]
        opts = jobs.normalize_export({**base, **{k: v for k, v in recipe.items() if k.startswith("video")},
                                      "root": body.get("root") or base["root"],
                                      "folder": body.get("folder", base["folder"])})
        dest = export_dest(opts)
        length = float(min(max(float(body.get("length") or 60), 10), 600))
        seg = float(min(max(float(body.get("segment") or 4), 2), 15))
        job = submit_reel(items, opts, dest, length, seg, str(body.get("title") or "").strip()[:60])
        return {"jobs": [job], "destination": str(dest)}

    @app.post("/api/match")
    def match_colours(body: dict = Body(...)):
        """Even out exposure and white balance across each trip's selected shots."""
        items = [item_or_404(m) for m in body.get("media_ids", [])]
        if len(items) < 2:
            raise HTTPException(400, "Select at least two shots of a trip")
        changed = tripmatch.match_library(lib, items)
        return {"checked": len(items), "changed": changed,
                "edits": {mid: lib.get_edit(mid) for mid in changed}}

    # -- culling --------------------------------------------------------------------
    @app.post("/api/cull")
    def cull(body: dict = Body(default={})):
        """Flag blurry shots and near-duplicates among the given photos (default: all), each
        compared with the rest of its trip. Returns counts and the reasons."""
        ids = body.get("media_ids")
        items = [item_or_404(m) for m in ids] if ids else lib.list()
        results = culling.cull_library(lib, items)
        rejected = {mid: r["reason"] for mid, r in results.items() if r["status"] == "reject"}
        return {"checked": len(results), "rejected": rejected}

    @app.put("/api/media/{mid}/cull")
    def set_cull(mid: str, body: dict = Body(...)):
        """Override culling: "keep", "reject", or null to go back to the automatic decision."""
        item_or_404(mid)
        override = body.get("override")
        if override not in ("keep", "reject", None):
            raise HTTPException(400, "override must be keep, reject or null")
        return lib.update_meta(mid, cull_override=override)

    @app.post("/api/media/{mid}/location")
    def detect_location(mid: str):
        """Look up "City, Country" from the item's GPS and save it in its edit."""
        item = item_or_404(mid)
        text = lib.detect_location(item)
        if not text:
            raise HTTPException(404, "No GPS location found in this file (or the lookup is offline)")
        edit = lib.get_edit(mid)
        edit = lib.set_edit(mid, {**edit, "location": {**edit["location"], "text": text}}, "Location found")
        return {"text": text, "edit": edit}

    @app.post("/api/media/{mid}/auto")
    def auto_adjust(mid: str, body: dict = Body(...)):
        """Suggest a level horizon (straighten) and/or exposure for the given edit."""
        item = item_or_404(mid)
        edit = normalize_edit(body.get("edit") if body.get("edit") is not None else lib.get_edit(mid))
        src = lib.preview_source(item, {**edit, "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}, 1200,
                                 float(body.get("t", 0) or 0))
        out = {}
        if body.get("horizon", True):
            pitch = geo.gimbal_pitch(lib.source_path(item))
            top_down = pitch is not None and pitch < recipes.TOP_DOWN_PITCH
            angle = None if top_down else analysis.auto_straighten(src, edit)
            out["horizon_found"] = angle is not None
            out["top_down"] = top_down
            out["straighten"] = angle if angle is not None else edit["straighten"]
        if body.get("exposure", True):
            out["exposure"] = analysis.auto_exposure(src, edit)
        return out

    # -- watch folder --------------------------------------------------------------
    def get_job(jid: str | None) -> dict | None:
        return queue.get(jid)

    watcher = Watcher(cfg, lib, load_settings, submit_export, export_dest, get_job, submit_reel)
    app.state.watcher = watcher
    watcher.start()

    @app.get("/api/watch")
    def get_watch():
        w = load_settings()["watch"]
        return {"settings": w, "status": watcher.status}

    @app.put("/api/watch")
    def put_watch(body: dict = Body(...)):
        before = load_settings()["watch"]
        w = normalize_watch({**before, **body})
        if w["enabled"]:
            if w["root"] not in cfg.input_roots:
                raise HTTPException(400, "Choose the NAS folder to watch")
            if (w["out_root"] or load_settings()["export"]["root"]) not in cfg.output_roots:
                raise HTTPException(400, "Choose where automatic exports go")
            try:
                if not watcher.folder(w).is_dir():
                    raise HTTPException(400, "That watch folder doesn't exist – is the NAS mounted?")
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        save_settings({"watch": w})
        started = w["enabled"] and (not before["enabled"] or before["root"] != w["root"]
                                    or before["folder"] != w["folder"])
        skipped = watcher.baseline() if started and not w["process_existing"] else 0
        watcher.poke()
        return {"settings": w, "status": watcher.status, "skipped_existing": skipped}

    @app.post("/api/watch/scan")
    def watch_scan():
        watcher.poke()
        return {"ok": True}

    @app.get("/api/jobs")
    def list_jobs():
        return queue.list()

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: str):
        if jid not in queue.jobs:
            raise HTTPException(404, "Job not found")
        queue.cancel(jid)
        return {"ok": True}

    @app.delete("/api/jobs")
    def clear_jobs():
        queue.clear_finished()
        return {"ok": True}

    # -- notifications ----------------------------------------------------------------
    def notify_info() -> dict:
        n = load_settings()["notify"]
        return {"settings": n, "channels": notify.channels(n), "smtp": bool(notify.smtp_settings()),
                "recent": watcher.status["notifications"]}

    @app.get("/api/notify")
    def get_notify():
        return notify_info()

    @app.put("/api/notify")
    def put_notify(body: dict = Body(...)):
        save_settings({"notify": body})
        return notify_info()

    @app.post("/api/notify/test")
    def test_notify():
        n = load_settings()["notify"]
        if not notify.channels(n):
            raise HTTPException(400, "Add an ntfy topic, a webhook or an email address first"
                                + ("" if notify.smtp_settings() else " (email also needs SIRIS_SMTP_HOST)"))
        thumbs = []
        for it in lib.list()[:3]:
            try:
                thumbs.append((f"{Path(it['name']).stem}.jpg", lib.thumb_path(it["id"]).read_bytes()))
            except OSError:
                pass
        results = notify.send({**n, "enabled": True}, "SirisDrone test",
                              "Notifications are working. You'll hear from SirisDrone when a trip is done.",
                              thumbs, {"test": True})
        return {"results": results}

    # -- status page ------------------------------------------------------------------
    def disk(path: Path) -> dict:
        try:
            u = shutil.disk_usage(path)
            return {"available": True, "free": u.free, "total": u.total}
        except OSError:
            return {"available": False}

    def ffmpeg_version() -> str:
        try:
            out = subprocess.run([video.FFMPEG, "-version"], capture_output=True, text=True, timeout=10).stdout
            return out.split("\n", 1)[0].replace("ffmpeg version ", "").split(" Copyright")[0]
        except (OSError, subprocess.SubprocessError):
            return "not found"

    @app.get("/api/status")
    def status():
        """Health at a glance: NAS space, watch folder, GPU, exports and recent problems."""
        w = load_settings()["watch"]
        items = lib.list()
        job_list = queue.list()
        failed = [j for j in job_list if j["status"] == "error"]
        problems = []
        outputs = [{"name": n, "path": str(p), **(disk(p) if p.is_dir() else {"available": False})}
                   for n, p in cfg.output_roots.items()]
        inputs = [{"name": n, "path": str(p), **(disk(p) if p.is_dir() else {"available": False})}
                  for n, p in cfg.input_roots.items()]
        for r in outputs + inputs:
            if not r["available"]:
                problems.append(f"{r['name']} ({r['path']}) isn't reachable – is the NAS mounted?")
            elif r["free"] < 20e9:
                problems.append(f"{r['name']} has only {r['free'] / 1e9:.1f} GB free")
        st = watcher.status
        recent_errors = [e for e in st["errors"] if time.time() - e["at"] < 24 * 3600]
        if w["enabled"]:
            if st["last_scan"] is None or time.time() - st["last_scan"] > max(3 * w["interval"], 300):
                problems.append("The watch folder hasn't been checked recently")
            if recent_errors:
                problems.append(f"Watch folder: {recent_errors[0]['message']}")
        if failed:
            problems.append(f"{len(failed)} export(s) failed – latest: {failed[-1]['name']}: {failed[-1]['error']}")
        g = gpu.detect()
        return {
            "version": __version__, "uptime": time.time() - started_at, "ok": not problems,
            "problems": problems, "gpu": g, "ffmpeg": ffmpeg_version(),
            "outputs": outputs, "inputs": inputs, "data": {"path": str(cfg.data_dir), **disk(cfg.data_dir)},
            "library": {"photos": sum(i["kind"] == "photo" for i in items),
                        "videos": sum(i["kind"] == "video" for i in items),
                        "rejected": sum(i["kind"] == "photo" and culling.effective(i) == "reject" for i in items)},
            "jobs": {s: sum(j["status"] == s for j in job_list) for s in ("queued", "running", "done", "error")},
            "watch": {"enabled": w["enabled"], "last_scan": st["last_scan"], "waiting": st["waiting"],
                      "processed": st["processed"], "errors": recent_errors[:5]},
            "notify": {"enabled": load_settings()["notify"]["enabled"],
                       "channels": notify.channels(load_settings()["notify"]),
                       "recent": st["notifications"][:5]},
        }

    # -- trip map ---------------------------------------------------------------------
    @app.get("/api/map")
    def trip_map():
        """Every shot with a known position, grouped by trip, plus each clip's flight path."""
        shots, tracks = [], {}
        for it in lib.list():
            it = lib.ensure_gps(it)
            if it["kind"] == "video":
                path = telemetry.track(lib.flight(it))
                if len(path) > 1:
                    tracks[it["id"]] = path
            if not it.get("gps"):
                continue
            shots.append({"id": it["id"], "name": it["name"], "kind": it["kind"], "lat": it["gps"][0],
                          "lon": it["gps"][1], "trip": lib.trip_key(it),
                          "rejected": culling.effective(it) == "reject"})
        return {"shots": shots, "tracks": tracks}

    @app.exception_handler(KeyError)
    async def key_error(request: Request, exc: KeyError):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    # -- phone upload: installable web app + Android share target ----------------------
    @app.get("/upload")
    def upload_page():
        return FileResponse(STATIC_DIR / "upload.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so it may control the whole app (needed to be installable).
        return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.post("/share")
    def share_target(files: list[UploadFile] = File(default=[])):
        """Android "Share → SirisDrone": add the files, then show the upload page."""
        r = upload(files) if files else {"added": [], "errors": [], "flight": []}
        n = len(r["added"]) + len(r["flight"])
        return RedirectResponse(f"/upload?shared={n}&errors={len(r['errors'])}", status_code=303)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
