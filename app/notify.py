"""Notifications: tell you when the watch folder has finished a trip (or something failed).

Channels, any combination (set in Automation → Notifications):

* **ntfy** – a topic URL such as ``https://ntfy.sh/brm-drone-1234`` (or a self-hosted ntfy).
  The phone app shows the summary with the first photo attached.
* **Webhook** – a URL that gets a JSON POST. Discord (``content``) and Slack (``text``)
  webhooks understand it as is; anything else can read ``title``, ``message`` and ``summary``.
* **Email** – to an address, through the SMTP server in the environment:
  ``SIRIS_SMTP_HOST``, ``SIRIS_SMTP_PORT`` (587 = STARTTLS, 465 = SSL), ``SIRIS_SMTP_USER``,
  ``SIRIS_SMTP_PASSWORD`` and ``SIRIS_SMTP_FROM``. The password stays in the environment, never
  in the settings file. Up to six thumbnails are included.
"""
from __future__ import annotations

import base64
import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

DEFAULT_NOTIFY = {
    "enabled": False,
    "ntfy_url": "",
    "webhook_url": "",
    "email_to": "",
}
TIMEOUT = 20


def normalize_notify(n: dict | None) -> dict:
    o = {**DEFAULT_NOTIFY, **{k: v for k, v in (n or {}).items() if k in DEFAULT_NOTIFY}}
    o["enabled"] = bool(o["enabled"])
    for k in ("ntfy_url", "webhook_url"):
        url = str(o[k] or "").strip()
        o[k] = url if url.startswith(("http://", "https://")) else ""
    to = str(o["email_to"] or "").strip()
    o["email_to"] = to if "@" in to and " " not in to else ""
    return o


def smtp_settings(env=None) -> dict | None:
    env = os.environ if env is None else env
    host = env.get("SIRIS_SMTP_HOST")
    if not host:
        return None
    port = int(env.get("SIRIS_SMTP_PORT") or 587)
    user = env.get("SIRIS_SMTP_USER") or ""
    return {"host": host, "port": port, "user": user, "password": env.get("SIRIS_SMTP_PASSWORD") or "",
            "from": env.get("SIRIS_SMTP_FROM") or user or "sirisdrone@localhost"}


def channels(n: dict) -> list[str]:
    out = []
    if n["ntfy_url"]:
        out.append("ntfy")
    if n["webhook_url"]:
        out.append("webhook")
    if n["email_to"] and smtp_settings():
        out.append("email")
    return out


def _header(value: str) -> str:
    """HTTP headers are Latin-1: send anything else RFC 2047-encoded (ntfy decodes it)."""
    try:
        value.encode("latin-1")
        return value.replace("\n", " ")
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


def send_ntfy(url: str, title: str, message: str, images: list[tuple[str, bytes]], tags: str) -> None:
    headers = {"Title": _header(title), "Tags": tags}
    if images:
        name, data = images[0]
        headers.update({"Message": _header(message), "Filename": name})
        req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    else:
        req = urllib.request.Request(url, data=message.encode(), headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def send_webhook(url: str, title: str, message: str, summary: dict) -> None:
    text = f"**{title}**\n{message}"
    body = {"title": title, "message": message, "summary": summary,
            "content": text[:1990], "text": text}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "SirisDrone-Studio"}, method="POST")
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def send_email(to: str, title: str, message: str, images: list[tuple[str, bytes]], smtp: dict) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = title, smtp["from"], to
    msg.set_content(message)
    cids = [f"img{i}@sirisdrone" for i in range(len(images))]
    html = "".join(f"<p>{line}</p>" for line in _html_escape(message).split("\n"))
    html += "".join(f'<img src="cid:{c}" style="width:200px;margin:4px;border-radius:6px">' for c in cids)
    msg.add_alternative(f"<html><body style='font-family:sans-serif'>{html}</body></html>", subtype="html")
    related = msg.get_payload()[1]
    for (name, data), cid in zip(images, cids):
        related.add_related(data, "image", "jpeg", cid=f"<{cid}>", filename=name)
    if smtp["port"] == 465:
        server = smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=TIMEOUT,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=TIMEOUT)
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
    try:
        if smtp["user"]:
            server.login(smtp["user"], smtp["password"])
        server.send_message(msg)
    finally:
        server.quit()


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(n: dict, title: str, message: str, images: list[tuple[str, bytes]] | None = None,
         summary: dict | None = None, failed: bool = False) -> dict[str, str]:
    """Send to every configured channel. Returns {channel: "sent" | error message}."""
    n = normalize_notify(n)
    images = (images or [])[:6]
    results: dict[str, str] = {}
    tags = "warning" if failed else "helicopter"
    for ch in channels(n):
        try:
            if ch == "ntfy":
                send_ntfy(n["ntfy_url"], title, message, images, tags)
            elif ch == "webhook":
                send_webhook(n["webhook_url"], title, message, summary or {})
            else:
                send_email(n["email_to"], title, message, images, smtp_settings())
            results[ch] = "sent"
        except Exception as exc:  # report, never break the caller
            results[ch] = f"{exc.__class__.__name__}: {exc}"[:300]
    return results


def trip_message(trip_name: str, rec: dict) -> tuple[str, str, bool]:
    """(title, message, failed) for a finished trip record from the watch folder."""
    parts = []
    if rec["photos"]:
        parts.append(f"{rec['photos']} photo{'s' * (rec['photos'] != 1)}")
    if rec["videos"]:
        parts.append(f"{rec['videos']} clip{'s' * (rec['videos'] != 1)}")
    lines = [("Exported " + " and ".join(parts) + ".") if parts else "Nothing exported."]
    if rec["rejected"]:
        lines.append(f"{rec['rejected']} skipped as blurry or duplicate (see Rejected in the library).")
    if rec.get("reel"):
        lines.append(f"Highlight reel: {rec['reel']}")
    if rec.get("folder"):
        lines.append(f"Saved to {rec['folder']}")
    for err in rec["errors"][:5]:
        lines.append(f"⚠ {err}")
    if len(rec["errors"]) > 5:
        lines.append(f"…and {len(rec['errors']) - 5} more problems")
    failed = bool(rec["errors"])
    title = f"{trip_name}: {'finished with problems' if failed else 'done'}"
    return title, "\n".join(lines), failed
