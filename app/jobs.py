"""Export queue: renders photos/videos one at a time and writes them to the NAS."""
from __future__ import annotations

import os
from collections import OrderedDict
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

import numpy as np
from PIL import Image

from . import geo, hdr, render, video
from .edits import normalize_edit

DEFAULT_EXPORT = {
    "root": None,             # name of an output root (e.g. "NAS")
    "folder": "",             # sub-folder inside it, created if missing
    "name_pattern": "{name}_BRM",  # tokens: {name} {date} {n}
    "photo_format": "jpeg",   # jpeg | png | tiff16
    "jpeg_quality": 95,
    "photo_size": "original", # "original", long-edge pixels, or a "WxH" box ("1080x1350", "x1080")
    "video_codec": "h264",    # h264 | h265 | prores
    "video_quality": "high",  # max | high | standard
    "video_size": "original",
    "video_fps": "source",    # "source", "max60" (source, capped at 60 for Apple TV) or a number
    "keep_metadata": True,
    "strip_gps": False,
    "overwrite": False,
    "use_gpu": True,          # NVENC/NVDEC when an NVIDIA GPU is available
}


NAME_TOKENS = ("{name}", "{date}", "{n}", "{place}")
_BAD_CHARS = r'[\\/:*?"<>|\x00-\x1f]'


def _parse_size(v):
    """"original" | long edge int | "WxH" box with either side optional."""
    v = str(v).strip().lower().replace(" ", "").replace("×", "x")
    if v.isdigit() and 64 <= int(v) <= 16384:
        return int(v)
    m = re.fullmatch(r"(\d*)x(\d*)", v)
    if m and any(m.groups()):
        w, h = (int(x) if x else None for x in m.groups())
        if all(d is None or 64 <= d <= 16384 for d in (w, h)):
            return f"{w or ''}x{h or ''}"
    return "original"


def size_spec(v):
    """Turn a normalised size option into what render/build_graph take."""
    if v == "original":
        return None
    if isinstance(v, int):
        return v
    w, h = v.split("x")
    return (int(w) if w else None, int(h) if h else None)


def normalize_export(opts: dict | None) -> dict:
    opts = dict(opts or {})
    if "name_pattern" not in opts and "suffix" in opts:  # settings saved by older versions
        opts["name_pattern"] = "{name}" + str(opts["suffix"] or "")
    o = {**DEFAULT_EXPORT, **{k: v for k, v in opts.items() if k in DEFAULT_EXPORT}}
    o["photo_format"] = o["photo_format"] if o["photo_format"] in render.PHOTO_FORMATS else "jpeg"
    o["video_codec"] = o["video_codec"] if o["video_codec"] in video.CODECS else "h264"
    o["video_quality"] = o["video_quality"] if o["video_quality"] in ("max", "high", "standard") else "high"
    o["jpeg_quality"] = int(min(max(float(o["jpeg_quality"] or 95), 50), 100))
    for k in ("photo_size", "video_size"):
        o[k] = _parse_size(o[k])
    f = str(o["video_fps"])
    if f not in ("source", "max60"):
        try:
            o["video_fps"] = float(f) if 1 <= float(f) <= 240 else "source"
        except ValueError:
            o["video_fps"] = "source"
    pattern = re.sub(_BAD_CHARS, "", str(o["name_pattern"] or "")).strip().strip(".")[:120]
    o["name_pattern"] = pattern or "{name}"
    o["folder"] = str(o["folder"] or "").strip().strip("/\\")
    for k in ("keep_metadata", "strip_gps", "overwrite", "use_gpu"):
        o[k] = bool(o[k])
    return o


def output_stem(pattern: str, name: str, date: str, n: int, place: str = "") -> str:
    stem = (pattern.replace("{name}", name).replace("{date}", date)
            .replace("{n}", f"{n:03d}").replace("{place}", place))
    stem = re.sub(_BAD_CHARS, "", stem)
    # tidy separators left behind by an empty token, e.g. "2026-10-04  IG" or "_ _"
    stem = re.sub(r"\s{2,}", " ", stem)
    stem = re.sub(r"([ _-])[ _-]+", r"\1", stem).strip(" _-.")
    return stem or name


def capture_date(exif_bytes=None, creation_time: str | None = None) -> str:
    """YYYY-MM-DD the shot was taken (EXIF / video metadata), else today."""
    if exif_bytes:
        try:
            from PIL import Image
            ex = Image.Exif()
            ex.load(exif_bytes)
            raw = ex.get_ifd(0x8769).get(36867) or ex.get(306)
            if raw:
                return str(raw)[:10].replace(":", "-")
        except Exception:
            pass
    if creation_time and re.match(r"\d{4}-\d{2}-\d{2}", creation_time):
        return creation_time[:10]
    return time.strftime("%Y-%m-%d")


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
        # Jobs cleared from the Exports list are kept here (most recent 1000) so the watch
        # folder can still confirm a finished export before deleting the original.
        self._cleared: OrderedDict[str, Job] = OrderedDict()
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
                self._cleared[jid] = self.jobs.pop(jid)
            while len(self._cleared) > 1000:
                self._cleared.popitem(last=False)

    def get(self, jid: str | None) -> dict | None:
        """A job by id, including ones cleared from the list; None if this app never ran it."""
        with self._lock:
            job = self.jobs.get(jid) or self._cleared.get(jid) if jid else None
        return job.public() if job else None

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


def _with_location(edit: dict, gps) -> dict:
    """Fill in an empty location stamp from GPS (items added before 1.1 have none yet)."""
    loc = edit["location"]
    if loc["enabled"] and not loc["text"]:
        text = geo.city_country(gps)
        if text:
            return {**edit, "location": {**loc, "text": text}}
    return edit


def export_photo(src_path: Path, name: str, edit: dict, opts: dict, dest: Path,
                 stamp_mask, job: Job, frame: tuple | None = None, n: int = 1) -> str:
    """Render a photo (or a video frame when `frame=(meta, t)`) at full quality."""
    edit = normalize_edit(edit)
    job.progress = 0.05
    if frame:
        meta, t = frame
        src = render.from_array(video.extract_frame(src_path, meta, t, bits=16))
        info = {}
        stem = f"{Path(name).stem}_{int(t // 60):02d}m{t % 60:05.2f}s".replace(".", "-")
        date = capture_date(creation_time=meta.get("creation_time"))
        gps = geo.gps_for_video(src_path, meta)
    else:
        src, info = render.load_photo(src_path)
        stem = Path(name).stem
        date = capture_date(info.get("exif"))
        gps = geo.gps_from_exif(info.get("exif"))
    if job.cancel.is_set():
        raise InterruptedError
    edit = _with_location(edit, gps)
    place = geo.place_name(gps) if "{place}" in opts["name_pattern"] else ""
    job.progress = 0.35
    ext = render.PHOTO_FORMATS[opts["photo_format"]]
    base = output_stem(opts["name_pattern"], stem, date, n, place)

    rgb = render.render(src, edit, long_edge=size_spec(opts["photo_size"]), stamp_mask=stamp_mask,
                        allow_upscale=True)
    del src
    job.progress = 0.8
    return _save(rgb, dest, base, ext, opts, info)


def _save(rgb, dest: Path, stem: str, ext: str, opts: dict, info: dict) -> str:
    final = _unique_path(dest, stem, ext, opts["overwrite"])
    tmp = dest / f".{final.name}.part"
    try:
        render.save_photo(rgb, tmp, opts["photo_format"], opts["jpeg_quality"], info,
                          opts["keep_metadata"], opts["strip_gps"])
        os.replace(tmp, final)  # atomic: no half-written files on the NAS
    finally:
        tmp.unlink(missing_ok=True)
    return str(final)


def export_video(src_path: Path, meta: dict, name: str, edit: dict, opts: dict, dest: Path,
                 stamp_mask, job: Job, n: int = 1) -> str:
    codec = opts["video_codec"]
    ext = video.CODECS[codec][1]
    place = (geo.place_name(geo.gps_for_video(src_path, meta))
             if "{place}" in opts["name_pattern"] else "")
    stem = output_stem(opts["name_pattern"], Path(name).stem,
                       capture_date(creation_time=meta.get("creation_time")), n, place)
    final = _unique_path(dest, stem, ext, opts["overwrite"])
    tmp = dest / f".{final.name}.part"

    def progress(p):
        job.progress = p

    edit = _with_location(normalize_edit(edit), geo.gps_for_video(src_path, meta))
    edit = video.resolve_stamp(src_path, meta, edit, stamp_mask)

    def attempt(use_gpu: bool) -> None:
        with tempfile.TemporaryDirectory(prefix="siris-") as work:
            _, pix, _ = video.encoder_settings(codec, opts["video_quality"], use_gpu)
            g = video.build_graph(
                meta, edit, Path(work), stamp_mask,
                long_edge=size_spec(opts["video_size"]), fps=output_fps(opts["video_fps"], meta),
                out_pix_fmt=pix, allow_upscale=True,
                stab=video.prepare_stabilize(src_path, meta, edit, Path(work), use_gpu, job.cancel))
            cmd = video.export_command(src_path, meta, g, tmp, codec, opts["video_quality"],
                                       opts["keep_metadata"], use_gpu=use_gpu)
            video.run_ffmpeg(cmd, g["duration"], progress, job.cancel)

    try:
        try:
            attempt(opts["use_gpu"])
        except video.FFmpegError:
            if not (opts["use_gpu"] and video.encoder_settings(codec, use_gpu=True)[2]):
                raise
            job.progress = 0.0
            attempt(False)  # GPU busy or failed (e.g. NVENC session limit): redo on the CPU
        os.replace(tmp, final)  # atomic: no half-written files on the NAS
    finally:
        tmp.unlink(missing_ok=True)
    return str(final)


def output_fps(option, meta: dict) -> float | None:
    if option == "max60":
        return 60.0 if (meta.get("fps") or 0) > 60.5 else None
    return None if option == "source" else float(option)


# ---------------------------------------------------------------------------
# Bracket merge and hyperlapse
# ---------------------------------------------------------------------------

def merge_brackets(paths: list[Path], names: list[str], add_to_library, job: Job) -> str:
    """Fuse AEB brackets and add the result to the library as a 16-bit TIFF."""
    loaded = []
    for i, p in enumerate(paths):
        loaded.append(render.load_photo(p))
        job.progress = 0.25 * (i + 1) / len(paths)
        if job.cancel.is_set():
            raise InterruptedError
    infos = [info for _, info in loaded]
    rgb = hdr.merge([src for src, _ in loaded], lambda p: setattr(job, "progress", 0.25 + 0.65 * p))
    del loaded
    exif = infos[len(infos) // 2].get("exif")
    name = f"{Path(sorted(names)[0]).stem}_HDR.tif"

    def write(dest: Path) -> None:
        import tifffile
        tifffile.imwrite(dest, (rgb * 65535 + 0.5).astype(np.uint16), photometric="rgb",
                         compression="zlib", software="SirisDrone Studio")
        if exif:
            dest.with_suffix(".exif").write_bytes(exif)

    item = add_to_library(name, ".tif", write)
    return f"Added {item['name']} to the library"


def export_hyperlapse(paths: list[Path], names: list[str], edit: dict, opts: dict, dest: Path,
                      stamp_mask, job: Job, fps: float = 25, stabilize: bool = True,
                      deflicker: bool = True) -> str:
    """Turn a photo sequence (e.g. Mini 3 Pro hyperlapse/interval shots) into a video.

    Every frame gets the same edit (grade, crop, sky filter...); the stamp is added once,
    after stabilisation, so it stays still."""
    edit = normalize_edit(edit)
    codec = opts["video_codec"]
    size = size_spec(opts["video_size"]) or (3840, 2160)
    order = sorted(range(len(paths)), key=lambda i: names[i])
    first_info = render.load_photo(paths[order[0]], 1024)[1]
    date = capture_date(first_info.get("exif"))
    place = (geo.place_name(geo.gps_from_exif(first_info.get("exif")))
             if "{place}" in opts["name_pattern"] else "")
    stem = output_stem(opts["name_pattern"], Path(names[order[0]]).stem + " hyperlapse", date, 1, place)
    final = _unique_path(dest, stem, video.CODECS[codec][1], opts["overwrite"])
    tmp = dest / f".{final.name}.part"
    frame_edit = {**edit, "stamp": {**edit["stamp"], "enabled": False}}
    with tempfile.TemporaryDirectory(prefix="siris-hl-") as work:
        work = Path(work)
        out_w = out_h = None
        for k, i in enumerate(order):
            if job.cancel.is_set():
                raise InterruptedError
            want = max(size) if isinstance(size, int) else max(d for d in size if d)
            src, _ = render.load_photo(paths[i], max_long_edge=int(want * 1.5))
            rgb = render.render(src, frame_edit, long_edge=size, allow_upscale=True)
            if out_w is None:
                out_h, out_w = (d - d % 2 for d in rgb.shape[:2])
            img = Image.fromarray(render.to_uint8(rgb)).resize((out_w, out_h), Image.LANCZOS) \
                if rgb.shape[:2] != (out_h, out_w) else Image.fromarray(render.to_uint8(rgb))
            img.save(work / f"f{k:05d}.png", compress_level=1)
            job.progress = 0.6 * (k + 1) / len(order)
        pattern = str(work / "f%05d.png")
        seq = ["-framerate", f"{fps}", "-i", pattern]
        pre = ["deflicker=size=7:mode=pm"] if deflicker and len(order) > 7 else []
        trf = None
        if stabilize and len(order) > 3:
            trf = work / "shake.trf"
            video.run_ffmpeg([video.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", *seq,
                              "-vf", ",".join(pre + [f"vidstabdetect=shakiness=8:accuracy=15:result='{video._esc(trf)}'"]),
                              "-f", "null", "-"], 0, None, job.cancel)
        chain = list(pre)
        if trf:
            chain.append(f"vidstabtransform=input='{video._esc(trf)}':smoothing=15:optzoom=1:interpol=bicubic")
        chain += ["scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd", "format=yuv444p10le"]
        graph = [f"[0:v]{','.join(chain)}[g]"]
        inputs, last = [], "g"
        if edit["stamp"]["enabled"] and stamp_mask is not None:
            st, x, y = render.render_stamp(stamp_mask, out_w, out_h, edit["stamp"])
            st.save(work / "stamp.png")
            inputs = ["-i", str(work / "stamp.png")]
            graph.append("[1:v]scale=out_color_matrix=bt709:out_range=tv,format=yuva444p10le[st]")
            graph.append(f"[g][st]overlay={x}:{y}:format=yuv444p10:eof_action=repeat[s]")
            last = "s"
        duration = len(order) / fps

        def attempt(use_gpu: bool) -> None:
            vargs, pix, _ = video.encoder_settings(codec, opts["video_quality"], use_gpu)
            cmd = [video.FFMPEG, "-hide_banner", "-y", "-nostdin", "-loglevel", "error", *seq, *inputs,
                   "-filter_complex", ";".join(graph + [f"[{last}]format={pix}[vout]"]),
                   "-map", "[vout]", *vargs, "-r", f"{fps}",
                   "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                   "-color_range", "tv", "-progress", "pipe:1", "-nostats"]
            if video.CODECS[codec][1] == ".mp4":
                cmd += ["-movflags", "+faststart"]
            cmd += ["-f", "mov" if video.CODECS[codec][1] == ".mov" else "mp4", str(tmp)]
            video.run_ffmpeg(cmd, duration, lambda p: setattr(job, "progress", 0.6 + 0.4 * p), job.cancel)

        try:
            try:
                attempt(opts["use_gpu"])
            except video.FFmpegError:
                if not (opts["use_gpu"] and video.encoder_settings(codec, use_gpu=True)[2]):
                    raise
                attempt(False)
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
    return str(final)
