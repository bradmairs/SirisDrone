"""In-app video playback.

* "original": the clip as shot. Served directly when the browser can play it,
  otherwise converted once to a 1280px H.264 copy (DJI 4K H.265 / 10-bit / D-Log).
* "edited": a quick low-res render of the current edit (grade, crop, straighten,
  trim, speed, vignette, stamp) through the same FFmpeg graph as the real export.

Renders run one at a time in the background; the browser polls for progress.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import telemetry, video
from .edits import normalize_edit

PLAYBACK_EDGE = 1280
KEEP_PER_ITEM = 3  # cached edited previews kept per clip


class Playback:
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playback")
        self._lock = threading.Lock()
        self.renders: dict[str, dict] = {}   # key -> {status, progress, path, error, cancel}

    def request(self, item: dict, src: Path, item_dir: Path, mode: str, edit: dict | None,
                stamp_mask, stamp_version: str) -> dict:
        """Return the state of a playable file for `mode`, starting a render if needed."""
        if mode == "original":
            if video.browser_playable(src, item):
                key = f"{item['id']}-src"
                with self._lock:
                    self.renders[key] = {"status": "ready", "progress": 1.0, "path": str(src)}
                return self._public(key, self.renders[key])
            key = f"{item['id']}-orig"
            out = item_dir / "playback" / "original.mp4"
            edit = normalize_edit({"grade": {}, "stamp": {"enabled": False}})
            mask = None
        else:
            edit = normalize_edit(edit)
            digest = hashlib.sha1(json.dumps([edit, stamp_version], sort_keys=True).encode()).hexdigest()[:12]
            key = f"{item['id']}-{digest}"
            out = item_dir / "playback" / f"edit-{digest}.mp4"
            mask = stamp_mask
        with self._lock:
            state = self.renders.get(key)
            if state and state["status"] in ("queued", "rendering", "ready"):
                if state["status"] != "ready" or Path(state["path"]).exists():
                    return self._public(key, state)
            if out.exists():
                state = {"status": "ready", "progress": 1.0, "path": str(out)}
                self.renders[key] = state
                return self._public(key, state)
            if mode == "edited":  # a newer edit supersedes any queued/running preview of this clip
                for k, st in self.renders.items():
                    if k.startswith(item["id"] + "-") and k != key and st["status"] in ("queued", "rendering") \
                            and not st.get("original"):
                        st["cancel"].set()
            state = {"status": "queued", "progress": 0.0, "path": str(out), "error": None,
                     "cancel": threading.Event(), "original": mode == "original"}
            self.renders[key] = state
        self._pool.submit(self._render, item, src, out, edit, mask, state)
        return self._public(key, state)

    def get(self, key: str) -> dict | None:
        state = self.renders.get(key)
        return self._public(key, state) if state else None

    @staticmethod
    def _public(key: str, st: dict) -> dict:
        return {"key": key, "status": st["status"], "progress": round(st.get("progress", 0), 3),
                "error": st.get("error"), "path": st.get("path")}

    def _render(self, item: dict, src: Path, out: Path, edit: dict, mask, state: dict) -> None:
        if state["cancel"].is_set():
            state["status"] = "cancelled"
            return
        state["status"] = "rendering"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.part")
        try:
            edit = video.resolve_stamp(src, item, edit, mask)

            def attempt(use_gpu: bool) -> None:
                with tempfile.TemporaryDirectory(prefix="siris-play-") as work:
                    _, pix, _ = video.encoder_settings("h264", "standard", use_gpu, fast=True)
                    stab = video.prepare_stabilize(src, item, edit, Path(work), use_gpu, state["cancel"])
                    g = video.build_graph(item, edit, Path(work), mask, long_edge=PLAYBACK_EDGE,
                                          out_pix_fmt=pix, stab=stab,
                                          flight=telemetry.load_for(src, out.parent.parent))
                    cmd = video.export_command(src, item, g, tmp, "h264", "standard",
                                               keep_metadata=False, fast=True, use_gpu=use_gpu)
                    video.run_ffmpeg(cmd, g["duration"], lambda p: state.__setitem__("progress", p),
                                     state["cancel"])

            try:
                attempt(True)
            except video.FFmpegError:
                if not video.encoder_settings("h264", use_gpu=True)[2]:
                    raise
                attempt(False)
            os.replace(tmp, out)
            state["status"], state["progress"] = "ready", 1.0
            self._prune(out.parent)
        except InterruptedError:
            state["status"] = "cancelled"
        except Exception as exc:
            state["status"], state["error"] = "error", str(exc)[-400:]
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _prune(folder: Path) -> None:
        edits = sorted(folder.glob("edit-*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in edits[KEEP_PER_ITEM:]:
            p.unlink(missing_ok=True)

    def wait(self, key: str, timeout: float = 120) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            st = self.get(key)
            if st and st["status"] not in ("queued", "rendering"):
                return st
            time.sleep(0.1)
        raise TimeoutError(key)
