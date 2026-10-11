"""SirisDrone Studio – web app for editing drone photos & videos and exporting them to a NAS."""
from __future__ import annotations

import base64
import json
import secrets
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analysis, geo, gpu, grading, jobs, recipes, render, video
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
    app = FastAPI(title="SirisDrone Studio", docs_url="/api/docs")
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
                "presets": presets, "watch": normalize_watch(s.get("watch"))}

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
        added, errors = [], []
        for f in files:
            try:
                added.append(lib.add_upload(f.filename or "upload", f.file, default_edit))
            except ValueError as exc:
                errors.append(str(exc))
        return {"added": added, "errors": errors}

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
        try:
            src = lib.preview_source(item, edit if mode != "frame" else
                                     {**edit, "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}, size, t)
            rgb = render.render(src, edit, long_edge=size, mode=mode, fast=True,
                                stamp_mask=stamp_mask(), allow_upscale=False)
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
                      frame_time: float | None = None, carousel: bool = False) -> dict:
        mask = stamp_mask()
        src = lib.source_path(item)
        if frame_time is not None and item["kind"] == "video":
            kind = "frame"
            run = lambda job: jobs.export_photo(src, item["name"], edit, opts, dest, mask, job,
                                                frame=(item, float(frame_time)), n=n)
        elif item["kind"] == "video":
            kind = "video"
            run = lambda job: jobs.export_video(src, item, item["name"], edit, opts, dest, mask, job, n=n)
        else:
            kind = "carousel" if carousel else "photo"
            run = lambda job: jobs.export_photo(src, item["name"], edit, opts, dest, mask, job,
                                                n=n, carousel=carousel)
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
        """One-click recipe export (Instagram post / carousel, Apple TV 4K, full resolution)."""
        name = body.get("recipe")
        if name not in recipes.RECIPES:
            raise HTTPException(400, "Unknown export recipe")
        base = load_settings()["export"]
        created, skipped = [], []
        dest = None
        for n, mid in enumerate(body.get("media_ids", []), start=1):
            item = item_or_404(mid)
            edit = lib.get_edit(mid)
            recipe = recipes.resolve(name, item, edit)
            if item["kind"] not in recipes.RECIPES[recipe]["kinds"]:
                skipped.append(item["name"])
                continue
            opts = jobs.normalize_export({**base, "root": body.get("root") or base["root"],
                                          "folder": body.get("folder", base["folder"]),
                                          **recipes.RECIPES[recipe]["options"]})
            dest = dest or export_dest(opts)
            edit = lib.set_edit(mid, recipes.recipe_edit(recipe, item, edit))  # show what was exported
            created.append(submit_export(item, edit, opts, dest, n, carousel=recipe == "carousel"))
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
        job = queue.jobs.get(jid) if jid else None
        return job.public() if job else None

    watcher = Watcher(cfg, lib, load_settings, submit_export, export_dest, get_job)
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

    @app.exception_handler(KeyError)
    async def key_error(request: Request, exc: KeyError):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
