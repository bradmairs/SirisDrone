"""Runtime configuration, read from environment variables.

SIRIS_DATA_DIR      Where uploads, edits and caches live (default: ./data)
SIRIS_OUTPUT_ROOTS  Export destinations, e.g. "NAS=/mnt/nas/Drone;Local=/home/me/Exports"
SIRIS_INPUT_ROOTS   Folders you can import media from without uploading,
                    e.g. "NAS Raw=/mnt/nas/DroneRaw"
SIRIS_USER / SIRIS_PASSWORD   Optional HTTP basic auth for the web UI
SIRIS_SMTP_*        Mail server for email notifications (see notify.py)
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
STATIC_DIR = APP_DIR / "static"
DEFAULT_STAMP = ASSETS_DIR / "brm_highlife.png"


def _parse_roots(value: str | None, fallback: dict[str, Path]) -> dict[str, Path]:
    if not value:
        return fallback
    roots: dict[str, Path] = {}
    for i, part in enumerate(p.strip() for p in value.replace("\n", ";").split(";")):
        if not part:
            continue
        name, sep, path = part.partition("=")
        if not sep:
            name, path = (f"Folder {i + 1}" if i else "NAS"), part
        roots[name.strip()] = Path(path.strip()).expanduser()
    return roots or fallback


class Config:
    def __init__(self, env: dict | None = None):
        env = os.environ if env is None else env
        self.data_dir = Path(env.get("SIRIS_DATA_DIR", "data")).expanduser().resolve()
        self.output_roots = _parse_roots(env.get("SIRIS_OUTPUT_ROOTS"),
                                         {"Exports": self.data_dir / "exports"})
        self.input_roots = _parse_roots(env.get("SIRIS_INPUT_ROOTS"), {})
        self.user = env.get("SIRIS_USER") or None
        self.password = env.get("SIRIS_PASSWORD") or None

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def lut_dir(self) -> Path:
        return self.data_dir / "luts"

    @property
    def tmp_dir(self) -> Path:
        """Upload spooling / FFmpeg scratch space: big files, so keep them off the system disk."""
        return self.data_dir / "tmp"

    @property
    def custom_stamp(self) -> Path:
        return self.data_dir / "stamp.png"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.lut_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)
        # Only create our own default export folder. Never create a NAS root: if the share
        # is not mounted that would silently send exports to the local disk instead.
        for root in self.output_roots.values():
            if root.resolve().is_relative_to(self.data_dir):
                root.mkdir(parents=True, exist_ok=True)


def resolve_in_root(roots: dict[str, Path], root: str, rel: str = "") -> Path:
    """Resolve `rel` inside the named root, refusing anything that escapes it."""
    if root not in roots:
        raise ValueError(f"Unknown folder '{root}'")
    base = roots[root].resolve()
    target = (base / (rel or "").lstrip("/\\")).resolve()
    if target != base and base not in target.parents:
        raise ValueError("Path is outside the allowed folder")
    return target
