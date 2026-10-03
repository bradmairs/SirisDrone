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

from . import grading, jobs, render, video
from .config import DEFAULT_STAMP, STATIC_DIR, Config, resolve_in_root
from .edits import DEFAULT_EDIT, normalize_edit
from .library import Library, media_kind
from .stamp import load_mask

MAX_PREVIEW = 2400


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config()
    cfg.ensure_dirs()
    tempfile.tempdir = str(cfg.tmp_dir)
    lib = Library(cfg.media_dir)
    queue = jobs.JobQueue()
    app = FastAPI(title="SirisDrone Studio", docs_url="/api/docs")
    app.state.cfg, app.state.lib, app.state.jobs = cfg, lib, queue

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
                "presets": presets}

    def save_settings(s: dict) -> dict:
        cur = load_settings()
        if "export" in s:
            cur["export"] = jobs.normalize_export({**cur["export"], **s["export"]})
        if "default_edit" in s:
            cur["default_edit"] = normalize_edit(s["default_edit"])
        if "presets" in s and isinstance(s["presets"], dict):
            cur["presets"] = s["presets"]
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
            "codecs": {k: v[0] for k, v in video.CODECS.items()},
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
            edit = normalize_edit({"rotate": edit["rotate"], "flip_h": edit["flip_h"],
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
    @app.post("/api/export")
    def export(body: dict = Body(...)):
        opts = jobs.normalize_export(body.get("options"))
        if opts["root"] not in cfg.output_roots:
            raise HTTPException(400, "Choose an export folder")
        if not cfg.output_roots[opts["root"]].is_dir():
            raise HTTPException(400, f"Export folder '{opts['root']}' is not reachable – is the NAS mounted?")
        try:
            dest = resolve_in_root(cfg.output_roots, opts["root"], opts["folder"])
            dest.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, f"Cannot use export folder: {exc}")
        save_settings({"export": opts})
        mask = stamp_mask()
        created = []
        frame_time = body.get("frame_time")
        for mid in body.get("media_ids", []):
            item = item_or_404(mid)
            edit = lib.get_edit(mid)
            src = lib.source_path(item)
            if frame_time is not None and item["kind"] == "video":
                t = float(frame_time)
                run = (lambda job, s=src, it=item, e=edit, t=t:
                       jobs.export_photo(s, it["name"], e, opts, dest, mask, job, frame=(it, t)))
                kind = "frame"
            elif item["kind"] == "video":
                run = (lambda job, s=src, it=item, e=edit:
                       jobs.export_video(s, it, it["name"], e, opts, dest, mask, job))
                kind = "video"
            else:
                run = (lambda job, s=src, it=item, e=edit:
                       jobs.export_photo(s, it["name"], e, opts, dest, mask, job))
                kind = "photo"
            created.append(queue.submit(mid, item["name"], kind, run).public())
        return {"jobs": created, "destination": str(dest)}

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
