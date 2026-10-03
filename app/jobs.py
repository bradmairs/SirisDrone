"""Export queue: renders photos/videos one at a time and writes them to the NAS."""
from __future__ import annotations

import os
import queue
import re
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import render, video
from .edits import normalize_edit

DEFAULT_EXPORT = {
    "root": None,             # name of an output root (e.g. "NAS")
    "folder": "",             # sub-folder inside it, created if missing
    "suffix": "_BRM",
    "photo_format": "jpeg",   # jpeg | png | tiff16
    "jpeg_quality": 95,
    "photo_size": "original", # "original" or long-edge pixels
    "video_codec": "h264",    # h264 | h265 | prores
    "video_quality": "high",  # max | high | standard
    "video_size": "original",
    "video_fps": "source",
    "keep_metadata": True,
    "strip_gps": False,
    "overwrite": False,
}


def normalize_export(opts: dict | None) -> dict:
    o = {**DEFAULT_EXPORT, **{k: v for k, v in (opts or {}).items() if k in DEFAULT_EXPORT}}
    o["photo_format"] = o["photo_format"] if o["photo_format"] in render.PHOTO_FORMATS else "jpeg"
    o["video_codec"] = o["video_codec"] if o["video_codec"] in video.CODECS else "h264"
    o["video_quality"] = o["video_quality"] if o["video_quality"] in ("max", "high", "standard") else "high"
    o["jpeg_quality"] = int(min(max(float(o["jpeg_quality"] or 95), 50), 100))
    for k in ("photo_size", "video_size"):
        v = str(o[k])
        o[k] = int(v) if v.isdigit() and 256 <= int(v) <= 16384 else "original"
    f = str(o["video_fps"])
    try:
        o["video_fps"] = float(f) if f != "source" and 1 <= float(f) <= 240 else "source"
    except ValueError:
        o["video_fps"] = "source"
    o["suffix"] = re.sub(r'[\\/:*?"<>|]', "", str(o["suffix"] or ""))[:40]
    o["folder"] = str(o["folder"] or "").strip().strip("/\\")
    for k in ("keep_metadata", "strip_gps", "overwrite"):
        o[k] = bool(o[k])
    return o


@dataclass
class Job:
    id: str
    media_id: str
    name: str
    kind: str                      # photo | video | frame
    status: str = "queued"         # queued | running | done | error | cancelled
    progress: float = 0.0
    output: str | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    run: Callable | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "media_id", "name", "kind", "status",
                                              "progress", "output", "error", "created", "finished")}


class JobQueue:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._q: queue.Queue[Job] = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True, name="exporter").start()

    def submit(self, media_id: str, name: str, kind: str, run: Callable[[Job], str]) -> Job:
        job = Job(uuid.uuid4().hex[:10], media_id, name, kind, run=run)
        with self._lock:
            self.jobs[job.id] = job
        self._q.put(job)
        return job

    def list(self) -> list[dict]:
        with self._lock:
            return [j.public() for j in sorted(self.jobs.values(), key=lambda j: j.created)]

    def cancel(self, jid: str) -> None:
        job = self.jobs[jid]
        job.cancel.set()
        if job.status == "queued":
            job.status, job.finished = "cancelled", time.time()

    def clear_finished(self) -> None:
        with self._lock:
            for jid in [j.id for j in self.jobs.values() if j.status in ("done", "error", "cancelled")]:
                del self.jobs[jid]

    def wait_idle(self, timeout: float = 120) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if all(j.status not in ("queued", "running") for j in self.jobs.values()):
                return
            time.sleep(0.05)
        raise TimeoutError("jobs still running")

    def _worker(self) -> None:
        while True:
            job = self._q.get()
            if job.cancel.is_set():
                continue
            job.status = "running"
            try:
                job.output = job.run(job)
                job.status, job.progress = "done", 1.0
            except InterruptedError:
                job.status = "cancelled"
            except Exception as exc:  # report, keep the queue alive
                job.status, job.error = "error", str(exc) or exc.__class__.__name__
                traceback.print_exc()
            finally:
                job.finished = time.time()
                job.run = None


# ---------------------------------------------------------------------------
# The export operations themselves
# ---------------------------------------------------------------------------

def _unique_path(folder: Path, stem: str, ext: str, overwrite: bool) -> Path:
    p = folder / f"{stem}{ext}"
    n = 2
    while not overwrite and p.exists():
        p = folder / f"{stem}-{n}{ext}"
        n += 1
    return p


def export_photo(src_path: Path, name: str, edit: dict, opts: dict, dest: Path,
                 stamp_mask, job: Job, frame: tuple | None = None) -> str:
    """Render a photo (or a video frame when `frame=(meta, t)`) at full quality."""
    edit = normalize_edit(edit)
    job.progress = 0.05
    if frame:
        meta, t = frame
        src = render.from_array(video.extract_frame(src_path, meta, t, bits=16))
        info = {}
        stem = f"{Path(name).stem}_{int(t // 60):02d}m{t % 60:05.2f}s".replace(".", "-")
    else:
        src, info = render.load_photo(src_path)
        stem = Path(name).stem
    if job.cancel.is_set():
        raise InterruptedError
    job.progress = 0.35
    long_edge = opts["photo_size"] if opts["photo_size"] != "original" else None
    rgb = render.render(src, edit, long_edge=long_edge, stamp_mask=stamp_mask, allow_upscale=True)
    del src
    job.progress = 0.8
    ext = render.PHOTO_FORMATS[opts["photo_format"]]
    final = _unique_path(dest, stem + opts["suffix"], ext, opts["overwrite"])
    tmp = dest / f".{final.name}.part"
    try:
        render.save_photo(rgb, tmp, opts["photo_format"], opts["jpeg_quality"], info,
                          opts["keep_metadata"], opts["strip_gps"])
        os.replace(tmp, final)  # atomic: no half-written files on the NAS
    finally:
        tmp.unlink(missing_ok=True)
    return str(final)


def export_video(src_path: Path, meta: dict, name: str, edit: dict, opts: dict, dest: Path,
                 stamp_mask, job: Job) -> str:
    codec = opts["video_codec"]
    ext = video.CODECS[codec][1]
    final = _unique_path(dest, Path(name).stem + opts["suffix"], ext, opts["overwrite"])
    tmp = dest / f".{final.name}.part"
    with tempfile.TemporaryDirectory(prefix="siris-") as work:
        g = video.build_graph(
            meta, edit, Path(work), stamp_mask,
            long_edge=opts["video_size"] if opts["video_size"] != "original" else None,
            fps=opts["video_fps"] if opts["video_fps"] != "source" else None,
            out_pix_fmt=video.CODECS[codec][3], allow_upscale=True)
        cmd = video.export_command(src_path, meta, g, tmp, codec, opts["video_quality"],
                                   opts["keep_metadata"])

        def progress(p):
            job.progress = p

        try:
            video.run_ffmpeg(cmd, g["duration"], progress, job.cancel)
            os.replace(tmp, final)  # atomic: no half-written files on the NAS
        finally:
            tmp.unlink(missing_ok=True)
    return str(final)
