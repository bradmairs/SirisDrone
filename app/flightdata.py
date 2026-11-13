"""Flight data overlay: altitude, speed, distance (and more) from the clip's DJI .SRT, drawn on
the video like a HUD.

The box sits in a corner (top-left by default, away from the BRM Highlife and location stamps)
in white with a soft dark halo, label above value, e.g.::

    ALT        SPEED        DIST
    52 m       36 km/h      120 m

The layout is worked out from the widest possible values so it never jiggles as the
numbers change. In exports it updates twice a second.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FIELDS = {          # key: (label, widest value for the layout)
    "alt": ("ALT", "8888 ft"),
    "speed": ("SPEED", "188 km/h"),
    "dist": ("DIST", "88.8 km"),
    "vspeed": ("V.SPEED", "-18.8 m/s"),
    "camera": ("CAMERA", "ISO 6400  1/8000  f/1.7"),
    "coords": ("POSITION", "-88.88888, -188.88888"),
    "time": ("TIME", "88:88:88"),
}
DEFAULT_FIELDS = ["alt", "speed", "dist"]
POSITIONS = ("tl", "tr", "bl", "br")
STEP = 0.5   # seconds between overlay updates in exports


def normalize(s: dict | None) -> dict:
    s = s or {}
    items = [k for k in (s.get("items") or DEFAULT_FIELDS) if k in FIELDS] or list(DEFAULT_FIELDS)

    def num(v, lo, hi, d):
        try:
            return min(max(float(v), lo), hi)
        except (TypeError, ValueError):
            return d
    return {"enabled": bool(s.get("enabled")), "items": list(dict.fromkeys(items)),
            "units": "imperial" if s.get("units") == "imperial" else "metric",
            "position": s.get("position") if s.get("position") in POSITIONS else "tl",
            "size": num(s.get("size"), 1.0, 6.0, 2.0), "margin": num(s.get("margin"), 0, 20, 2.7),
            "opacity": num(s.get("opacity"), 10, 100, 90)}


def _dist(m: float, imperial: bool) -> str:
    if imperial:
        ft = m * 3.28084
        return f"{ft:.0f} ft" if ft < 5280 else f"{m / 1609.344:.1f} mi"
    return f"{m:.0f} m" if m < 1000 else f"{m / 1000:.1f} km"


def values(row: dict | None, s: dict) -> list[tuple[str, str]]:
    """[(label, value)] for the chosen fields; "–" where the drone didn't record it."""
    imp = s["units"] == "imperial"
    row = row or {}
    out = []
    for key in s["items"]:
        label = FIELDS[key][0]
        v = "–"
        if key == "alt" and "alt" in row:
            v = f"{row['alt'] * 3.28084:.0f} ft" if imp else f"{row['alt']:.0f} m"
        elif key == "speed" and "speed" in row:
            v = f"{row['speed'] * 2.23694:.0f} mph" if imp else f"{row['speed'] * 3.6:.0f} km/h"
        elif key == "dist" and "dist" in row:
            v = _dist(row["dist"], imp)
        elif key == "vspeed" and "vspeed" in row:
            v = f"{row['vspeed'] * 3.28084:+.0f} ft/s" if imp else f"{row['vspeed']:+.1f} m/s"
        elif key == "camera" and ("iso" in row or "shutter" in row):
            parts = []
            if "iso" in row:
                parts.append(f"ISO {row['iso']:.0f}")
            if "shutter" in row:
                parts.append(row["shutter"].removesuffix(".0"))
            if "fnum" in row:
                parts.append(f"f/{row['fnum']:g}")
            v = "  ".join(parts)
        elif key == "coords" and "lat" in row:
            v = f"{row['lat']:.5f}, {row['lon']:.5f}"
        elif key == "time" and row.get("time"):
            v = row["time"][11:19]
        out.append((label, v))
    return out


def render_box(vals: list[tuple[str, str]], out_w: int, out_h: int, s: dict) -> Image.Image:
    """The overlay as an RGBA image, its size fixed by the chosen fields (not the values)."""
    short = min(out_w, out_h)
    vsize = max(8, round(short * s["size"] / 100))
    lsize = max(6, round(vsize * 0.55))
    vfont, lfont = ImageFont.load_default(size=vsize), ImageFont.load_default(size=lsize)
    gap = round(vsize * 1.1)
    widths = [max(vfont.getlength(FIELDS[k][1]), lfont.getlength(FIELDS[k][0])) for k in s["items"]]
    pad = max(2, round(vsize * 0.35))
    w = int(sum(widths) + gap * (len(widths) - 1) + 2 * pad)
    h = int(lsize * 1.25 + vsize * 1.2 + 2 * pad)
    text = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(text)
    right = s["position"] in ("tr", "br")
    x = pad
    for (label, value), cw in zip(vals, widths):
        ax = x + cw if right else x
        anchor_l, anchor_v = ("ra", "ra") if right else ("la", "la")
        d.text((ax, pad), label, font=lfont, fill=190, anchor=anchor_l)
        d.text((ax, pad + lsize * 1.25), value, font=vfont, fill=255, anchor=anchor_v,
               stroke_width=max(0, vsize // 28), stroke_fill=255)
        x += cw + gap
    halo = text.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(max(1.0, vsize * 0.12)))
    alpha = Image.eval(halo, lambda v: min(255, int(v * 1.6 * 0.55)))
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.putalpha(alpha)                                    # dark halo
    white = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    white.putalpha(text)
    img = Image.alpha_composite(img, white)
    if s["opacity"] < 100:
        img.putalpha(Image.eval(img.getchannel("A"), lambda v: int(v * s["opacity"] / 100)))
    return img


def place(img: Image.Image, out_w: int, out_h: int, s: dict) -> tuple[int, int]:
    m = round(min(out_w, out_h) * s["margin"] / 100)
    x = m if s["position"] in ("tl", "bl") else out_w - img.width - m
    y = m if s["position"] in ("tl", "tr") else out_h - img.height - m
    return max(0, x), max(0, y)


def frames(rows: list[dict], s: dict, out_w: int, out_h: int, start: float, duration: float,
           speed: float, workdir: Path) -> tuple[Path, int, int] | None:
    """PNG frames + an FFmpeg concat list for an export: (list file, x, y), or None.

    Times are output seconds: the clip is trimmed to `start` and sped up by `speed`."""
    from .telemetry import at
    if not rows:
        return None
    n = max(1, int(duration / STEP + 0.999))
    entries: list[list] = []
    last = None
    for k in range(n):
        vals = values(at(rows, start + k * STEP * speed), s)
        if vals == last:
            entries[-1][1] += STEP
            continue
        last = vals
        p = workdir / f"flight{len(entries):05d}.png"
        render_box(vals, out_w, out_h, s).save(p)
        entries.append([p, STEP])
    lst = workdir / "flight.txt"
    lines = []
    for p, dur in entries:
        lines += [f"file '{p.name}'", f"duration {dur:.3f}"]
    lines.append(f"file '{entries[-1][0].name}'")  # the concat demuxer needs the last one twice
    lst.write_text("\n".join(lines) + "\n")
    first = Image.open(entries[0][0])
    x, y = place(first, out_w, out_h, s)
    return lst, x, y
