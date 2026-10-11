"""Bump the SirisDrone Studio version and open a CHANGELOG entry.

    python tools/bump_version.py small "Fix stamp margin on vertical crops"
    python tools/bump_version.py big   "Watch folder for video"

small = +0.01 (fixes, tweaks, small additions); big = +0.1 (significant features).
The version is plain decimal arithmetic: 1.0 -> 1.01 -> 1.02 ... and 1.02 -> 1.12 for a big step.
"""
from __future__ import annotations

import datetime
import re
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "app" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
STEPS = {"small": Decimal("0.01"), "big": Decimal("0.1")}


def fmt(v: Decimal) -> str:
    """1 -> "1.0", 1.10 -> "1.1", 1.01 -> "1.01"."""
    s = format(v.normalize(), "f")
    return s if "." in s else s + ".0"


def current() -> str:
    return re.search(r'__version__ = "([\d.]+)"', INIT.read_text()).group(1)


def bump(kind: str, summary: str = "", today: str | None = None) -> str:
    new = fmt(Decimal(current()) + STEPS[kind])
    INIT.write_text(re.sub(r'__version__ = "[\d.]+"', f'__version__ = "{new}"', INIT.read_text()))
    today = today or datetime.date.today().isoformat()
    text = CHANGELOG.read_text()
    entry = f"## {new} – {today}\n\n- {summary or 'TODO: describe the change'}\n\n"
    i = text.index("## ")  # newest entry goes first
    CHANGELOG.write_text(text[:i] + entry + text[i:])
    return new


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in STEPS:
        sys.exit(__doc__)
    print(bump(sys.argv[1], " ".join(sys.argv[2:])))
