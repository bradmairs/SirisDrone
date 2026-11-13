"""Trip highlight reel: the best few seconds of each clip, cut together with crossfades.

1. **Score** – every clip is sampled twice a second at a tiny size. Each sample gets a score
   for sharpness (mean edge strength, relative to the trip) minus penalties for jerky motion
   (how much the frame-to-frame movement changes, found by phase correlation) and for very
   fast pans. The first and last second of a clip (take-off wobble, gimbal settling) are
   skipped.
2. **Pick** – the best window of each clip first, so every clip is represented, then the next
   best windows anywhere until the reel is long enough; never two overlapping windows.
3. **Render** – each window is rendered with its clip's own edit (grade, crop, stabilise,
   denoise; no stamp) at the reel size, letterboxed if its crop isn't 16:9. Then a title card
   (place and date over a blurred first frame) and the clips are joined with crossfades, the
   BRM Highlife stamp is added once over the clips, and it fades to black at the end.

Clips are taken in shooting order, so the reel follows the flight.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from . import gpu, render, video
from .edits import normalize_edit
from .jobs import Job, _unique_path, capture_date, output_fps, size_spec
from .stamp import render_stamp

SAMPLE_FPS = 2.0
SAMPLE_W = 192
EDGE_SKIP = 1.0       # seconds left out at each end of a clip
JERK_WEIGHT = 0.08    # score lost per pixel (at SAMPLE_W) of change in motion between samples
FAST_PAN = 0.15       # pans faster than this fraction of the width per sample are penalised
TRANSITION = 0.75     # crossfade seconds
TITLE_SECONDS = 3.0
LENGTHS = (30, 60, 90, 120)
SEGMENTS = (3.0, 4.0, 5.0, 6.0)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def sample_frames(path: Path, meta: dict, start: float, duration: float,
                  use_gpu: bool = False) -> np.ndarray:
    """Grey frames, SAMPLE_FPS per second, SAMPLE_W wide: array (n, h, w) float32."""
    w = SAMPLE_W
    h = max(2, round(SAMPLE_W * meta["height"] / meta["width"] / 2) * 2)
    cmd = [video.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", *gpu.decode_args(use_gpu),
           "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path), "-an", "-sn",
           "-vf", f"fps={SAMPLE_FPS},scale={w}:{h}:flags=area,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0 and use_gpu:
        return sample_frames(path, meta, start, duration, False)
    if res.returncode != 0:
        raise video.FFmpegError(f"Could not read {path.name}: {res.stderr.decode(errors='replace')[-300:]}")
    n = len(res.stdout) // (w * h)
    return np.frombuffer(res.stdout[: n * w * h], np.uint8).reshape(n, h, w).astype(np.float32)


def shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Translation (dy, dx) from frame a to frame b, by phase correlation."""
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1])).astype(np.float32)
    fa, fb = np.fft.fft2((a - a.mean()) * win), np.fft.fft2((b - b.mean()) * win)
    r = fb * np.conj(fa)
    r /= np.abs(r) + 1e-6
    c = np.fft.ifft2(r).real
    dy, dx = np.unravel_index(int(np.argmax(c)), c.shape)
    h, w = c.shape
    return float(dy - h if dy > h // 2 else dy), float(dx - w if dx > w // 2 else dx)


def sharpness(f: np.ndarray) -> float:
    return float(np.abs(np.diff(f, axis=0)).mean() + np.abs(np.diff(f, axis=1)).mean())


def sample_scores(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(sharpness, motion penalty) per sample."""
    n = len(frames)
    sharp = np.array([sharpness(f) for f in frames], np.float32)
    moves = np.zeros((n, 2), np.float32)
    for i in range(1, n):
        moves[i] = shift(frames[i - 1], frames[i])
    if n > 1:
        moves[0] = moves[1]
    jerk = np.zeros(n, np.float32)
    jerk[1:] = np.linalg.norm(np.diff(moves, axis=0), axis=1)
    width = frames.shape[2] if n else SAMPLE_W
    speed = np.linalg.norm(moves, axis=1) / width
    penalty = JERK_WEIGHT * jerk + 4.0 * np.maximum(speed - FAST_PAN, 0)
    return sharp, penalty


def windows(times: np.ndarray, scores: np.ndarray, seg: float, lo: float, hi: float) -> list[tuple[float, float]]:
    """Candidate (start, score) windows of `seg` source-seconds inside [lo, hi]."""
    if hi - lo < seg:
        if hi - lo < 1.5:
            return []
        inside = (times >= lo) & (times < hi)
        return [(lo, float(scores[inside].mean()) if inside.any() else 0.0)]
    out = []
    t = lo
    while t + seg <= hi + 1e-6:
        inside = (times >= t) & (times < t + seg)
        if inside.any():
            # The worst moment matters too: one jolt spoils a shot.
            out.append((t, float(0.7 * scores[inside].mean() + 0.3 * scores[inside].min())))
        t += 1 / SAMPLE_FPS
    return out


def plan(clips: list[dict], length: float, seg: float) -> list[dict]:
    """Choose segments. `clips`: dicts with "times", "scores" (source seconds), "lo", "hi" and
    "speed". Returns [{"clip": index, "start": s, "end": e}] in shooting order."""
    slots = max(1, int((length - TITLE_SECONDS) // max(seg - TRANSITION, 0.5)))
    per_clip = max(2, math.ceil(slots / max(len(clips), 1)) + 1)
    cands: list[tuple[float, int, float, float]] = []
    for ci, c in enumerate(clips):
        span = seg * c["speed"]
        for start, score in windows(c["times"], c["scores"], span, c["lo"], c["hi"]):
            cands.append((score, ci, start, min(start + span, c["hi"])))
    chosen: list[tuple[float, int, float, float]] = []

    def free(cand) -> bool:
        _, ci, s, e = cand
        return all(not (o[1] == ci and s < o[3] and o[2] < e) for o in chosen)

    best_per_clip: dict[int, tuple] = {}
    for cand in cands:
        if cand[1] not in best_per_clip or cand[0] > best_per_clip[cand[1]][0]:
            best_per_clip[cand[1]] = cand
    for cand in sorted(best_per_clip.values(), reverse=True)[:slots]:
        chosen.append(cand)
    for cand in sorted(cands, reverse=True):
        if len(chosen) >= slots:
            break
        if free(cand) and sum(o[1] == cand[1] for o in chosen) < per_clip:
            chosen.append(cand)
    chosen.sort(key=lambda c: (c[1], c[2]))
    return [{"clip": ci, "start": round(s, 3), "end": round(e, 3), "score": round(sc, 3)}
            for sc, ci, s, e in chosen]


def score_clip(path: Path, meta: dict, edit: dict, use_gpu: bool = False) -> dict:
    v = normalize_edit(edit)["video"]
    lo = v["trim_start"]
    hi = v["trim_end"] if v["trim_end"] is not None else meta["duration"]
    hi = min(hi, meta["duration"])
    frames = sample_frames(path, meta, lo, max(hi - lo, 0.1), use_gpu)
    times = lo + np.arange(len(frames)) / SAMPLE_FPS
    sharp, penalty = sample_scores(frames) if len(frames) else (np.zeros(0), np.zeros(0))
    skip = EDGE_SKIP if hi - lo > 2 * EDGE_SKIP + 2 else 0.0
    return {"times": times, "sharp": sharp, "penalty": penalty, "lo": lo + skip, "hi": hi - skip,
            "speed": v["speed"]}


def finish_scores(scored: list[dict]) -> list[dict]:
    """Sharpness relative to the whole trip (so a misty clip isn't compared with itself)."""
    allsharp = np.concatenate([c["sharp"] for c in scored if len(c["sharp"])] or [np.ones(1)])
    ref = float(np.median(allsharp)) or 1.0
    for c in scored:
        c["scores"] = np.minimum(c["sharp"] / ref, 1.5) - c["penalty"]
    return scored


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def fps_arg(fps: float) -> str:
    for ntsc in (24, 30, 60):
        if abs(fps - ntsc * 1000 / 1001) < 0.01:
            return f"{ntsc * 1000}/1001"
    return f"{fps:.3f}".rstrip("0").rstrip(".")


def pick_fps(metas: list[dict], option) -> float:
    fpss = [round(m.get("fps") or 30, 2) for m in metas]
    common = max(set(fpss), key=fpss.count)
    want = output_fps(option, {"fps": common})
    return min(want or common, 60.0)


def _font(size: int):
    return ImageFont.load_default(size=size)


def title_text(title: str, location: str) -> tuple[str, str]:
    """("WHARARIKI BEACH", "NEW ZEALAND") from a title or "Town, Country" location text."""
    if title:
        return title.upper(), ""
    if "," in location:
        town, country = location.rsplit(",", 1)
        return town.strip().upper(), country.strip().upper()
    return (location or "Highlights").upper(), ""


def nice_date(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso)
        return f"{d.day} {d.strftime('%B %Y')}".upper()
    except ValueError:
        return ""


def title_card(frame: np.ndarray, size: tuple[int, int], heading: str, sub: str,
               stamp_mask: Image.Image | None) -> Image.Image:
    """Blurred, darkened first frame with the place, date and BRM Highlife logo."""
    w, h = size
    canvas = ImageOps.fit(Image.fromarray(render.to_uint8(frame)), (w, h), Image.LANCZOS)
    canvas = canvas.filter(ImageFilter.GaussianBlur(max(w, h) / 160))
    canvas = Image.eval(canvas, lambda v: int(v * 0.5))
    d = ImageDraw.Draw(canvas)
    big = _font(max(12, round(h * 0.075)))
    small = _font(max(8, round(h * 0.028)))
    y = h * 0.42
    d.text((w / 2, y), heading, font=big, fill=(255, 255, 255), anchor="ms")
    if sub:
        d.text((w / 2, y + h * 0.06), sub, font=small, fill=(225, 230, 240), anchor="ms")
    if stamp_mask is not None:
        st, _, _ = render_stamp(stamp_mask, w, h, {"enabled": True, "position": "bc", "size": 30,
                                                    "margin": 0, "opacity": 100, "color": "#ffffff",
                                                    "shadow": 0})
        canvas.paste(st.convert("RGB"), ((w - st.width) // 2, round(h * 0.60)), st.getchannel("A"))
    return canvas


def _encode_attempts(make_cmd, duration: float, on_progress, job: Job, use_gpu: bool) -> None:
    """Run make_cmd(use_gpu); if the GPU encoder fails, redo on the CPU."""
    try:
        video.run_ffmpeg(make_cmd(use_gpu), duration, on_progress, job.cancel)
    except video.FFmpegError:
        if not use_gpu:
            raise
        video.run_ffmpeg(make_cmd(False), duration, on_progress, job.cancel)


def render_segment(path: Path, meta: dict, edit: dict, start: float, end: float, size: tuple[int, int],
                   fps: float, codec: str, out: Path, work: Path, job: Job, use_gpu: bool,
                   on_progress) -> float:
    """One window of a clip, letterboxed to `size`. Returns its length in seconds."""
    e = normalize_edit(edit)
    e["video"] = {**e["video"], "trim_start": start, "trim_end": end, "mute": True}
    e["stamp"] = {**e["stamp"], "enabled": False}
    e["location"] = {**e["location"], "enabled": False}
    e["flight"] = {**e["flight"], "enabled": False}
    w, h = size

    def make(gpu_on: bool) -> list[str]:
        vargs, pix, on_gpu = video.encoder_settings(codec, "max", gpu_on)
        stab = video.prepare_stabilize(path, meta, e, work, gpu_on, job.cancel)
        g = video.build_graph(meta, e, work, None, long_edge=(w, h), fps=fps,
                              out_pix_fmt="yuv444p10le", allow_upscale=True, stab=stab)
        graph = g["filter"] + f";[vout]pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format={pix}[p]"
        make.duration = g["duration"]
        return [video.FFMPEG, "-hide_banner", "-y", "-nostdin", "-loglevel", "error",
                *gpu.decode_args(on_gpu), "-ss", f"{g['start']:.3f}", "-t", f"{g['src_duration']:.3f}",
                "-i", str(path), *g["inputs"], "-filter_complex", graph, "-map", "[p]", "-an", *vargs,
                "-r", fps_arg(fps), "-colorspace", "bt709", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-color_range", "tv", "-progress", "pipe:1", "-nostats",
                "-f", "mov" if video.CODECS[codec][1] == ".mov" else "mp4", str(out)]

    make.duration = (end - start) / e["video"]["speed"]
    _encode_attempts(make, make.duration, on_progress, job, use_gpu)
    return float(video.probe(out).get("duration") or make.duration)


def make_reel(clips: list[dict], opts: dict, dest: Path, stamp_mask, job: Job, length: float = 60,
              seg: float = 4.0, title: str = "", stem: str | None = None, overwrite: bool = False) -> str:
    """clips: [{"path", "meta" (library item), "name", "edit"}]. Returns the output path."""
    if not clips:
        raise ValueError("No clips for a highlight reel")
    order = sorted(range(len(clips)), key=lambda i: (clips[i]["meta"].get("creation_time") or "",
                                                     clips[i]["name"]))
    clips = [clips[i] for i in order]
    codec = opts["video_codec"]
    use_gpu = bool(opts.get("use_gpu"))
    metas = [c["meta"] for c in clips]
    size = size_spec(opts["video_size"])
    if not isinstance(size, tuple) or not all(size):
        m = metas[0]
        size = (m["width"] - m["width"] % 2, m["height"] - m["height"] % 2)
    fps = pick_fps(metas, opts["video_fps"])

    scored = []
    for k, c in enumerate(clips):
        if job.cancel.is_set():
            raise InterruptedError
        scored.append(score_clip(c["path"], c["meta"], c["edit"], use_gpu))
        job.progress = 0.2 * (k + 1) / len(clips)
    picks = plan(finish_scores(scored), length, seg)
    if not picks:
        raise ValueError("The clips are too short for a highlight reel")

    first = clips[picks[0]["clip"]]
    date = capture_date(creation_time=first["meta"].get("creation_time"))
    loc = normalize_edit(first["edit"])["location"]["text"]
    heading, country = title_text(title, loc)
    sub = " · ".join(p for p in (country, nice_date(date)) if p)
    name = stem or f"{date} {(title or loc.split(',')[0] or 'Trip').strip()} highlights"
    ext = video.CODECS[codec][1]
    final = _unique_path(dest, name, ext, overwrite)
    tmp = dest / f".{final.name}.part"

    with tempfile.TemporaryDirectory(prefix="siris-reel-") as work:
        work = Path(work)
        segs, lens = [], []
        for k, p in enumerate(picks):
            if job.cancel.is_set():
                raise InterruptedError
            c = clips[p["clip"]]
            sdir = work / f"s{k}"
            sdir.mkdir()
            out = work / f"seg{k}{ext}"
            base = 0.2 + 0.6 * k / len(picks)
            lens.append(render_segment(c["path"], c["meta"], c["edit"], p["start"], p["end"], size, fps,
                                       codec, out, sdir, job, use_gpu,
                                       lambda q, base=base: setattr(job, "progress", base + 0.6 * q / len(picks))))
            segs.append(out)

        frame = render.render(render.from_array(video.extract_frame(first["path"], first["meta"],
                                                                     picks[0]["start"], 1920)),
                              {**normalize_edit(first["edit"]), "stamp": {"enabled": False},
                               "location": {"enabled": False}}, long_edge=1920)
        card = work / "title.png"
        title_card(frame, size, heading, sub, stamp_mask).save(card)

        t = min(TRANSITION, min(lens) / 3, TITLE_SECONDS / 3)
        durs = [TITLE_SECONDS] + lens
        total = sum(durs) - t * (len(durs) - 1)
        fa = fps_arg(fps)
        inputs = ["-loop", "1", "-framerate", fa, "-t", f"{TITLE_SECONDS}", "-i", str(card)]
        for s in segs:
            inputs += ["-i", str(s)]
        norm = f"settb=AVTB,fps={fa},format=yuv420p10le,setsar=1"
        graph = [f"[0:v]scale=out_color_matrix=bt709:out_range=tv,{norm},fade=t=in:d=0.6[v0]"]
        graph += [f"[{k}:v]{norm}[v{k}]" for k in range(1, len(durs))]
        last, offset = "v0", 0.0
        for k in range(1, len(durs)):
            offset += durs[k - 1] - t
            graph.append(f"[{last}][v{k}]xfade=transition=fade:duration={t:.3f}:offset={offset:.3f}[x{k}]")
            last = f"x{k}"
        graph.append(f"[{last}]fade=t=out:st={max(total - 1.2, 0):.3f}:d=1.2,format=yuv444p10le[f]")
        last = "f"
        if stamp_mask is not None:
            st_edit = normalize_edit(first["edit"])["stamp"]
            if st_edit["enabled"]:
                st_edit = {**st_edit, "color": "#ffffff" if st_edit["color"] == "auto" else st_edit["color"]}
                img, x, y = render_stamp(stamp_mask, size[0], size[1], st_edit)
                img.save(work / "stamp.png")
                inputs += ["-i", str(work / "stamp.png")]
                n = len(durs)
                graph.append(f"[{n}:v]scale=out_color_matrix=bt709:out_range=tv,format=yuva444p10le[st]")
                graph.append(f"[f][st]overlay={x}:{y}:format=yuv444p10:eof_action=repeat"
                             f":enable='gte(t,{TITLE_SECONDS:.2f})'[s]")
                last = "s"

        def make(gpu_on: bool) -> list[str]:
            vargs, pix, _ = video.encoder_settings(codec, opts["video_quality"], gpu_on)
            cmd = [video.FFMPEG, "-hide_banner", "-y", "-nostdin", "-loglevel", "error", *inputs,
                   "-filter_complex", ";".join(graph + [f"[{last}]format={pix}[vout]"]),
                   "-map", "[vout]", "-an", *vargs, "-r", fa, "-t", f"{total:.3f}",
                   "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                   "-color_range", "tv", "-metadata", f"title={heading}", "-progress", "pipe:1", "-nostats"]
            if ext == ".mp4":
                cmd += ["-movflags", "+faststart"]
            return cmd + ["-f", "mov" if ext == ".mov" else "mp4", str(tmp)]

        try:
            _encode_attempts(make, total, lambda q: setattr(job, "progress", 0.8 + 0.2 * q), job, use_gpu)
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
    return str(final)
