"""Release D: telemetry, trip map, status page and notifications."""
import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image, ImageFilter

from app import notify, telemetry
from tests.conftest import make_video
from tests.scenes import jitter, scene, shaken

MINI3 = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2026-04-03 10:12:31.123
[iso: 100] [shutter: 1/1000.0] [fnum: 1.7] [ev: 0] [color_md: default] [focal_len: 24.00] [latitude: {lat:.6f}] [longitude: 145.000000] [rel_alt: {alt:.3f} abs_alt: 120.500] [ct: 5500] </font>
"""


def mini3_srt(seconds=5, fps=2, speed=10.0, climb=2.0):
    """A Mini 3 Pro style .SRT flying north at `speed` m/s while climbing."""
    blocks = []
    for i in range(seconds * fps + 1):
        t = i / fps
        lat = -38.0 + speed * t / 111195.0
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        stamp = f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"
        blk = MINI3.format(lat=lat, alt=50 + climb * t).replace("00:00:00,000 --> 00:00:00,033",
                                                                 f"{stamp} --> {stamp}")
        blocks.append(blk.replace("1\n", f"{i + 1}\n", 1))
    return "\n".join(blocks)


def test_parse_mini3_srt_and_derive_speed():
    rows = telemetry.enrich(telemetry.parse(mini3_srt()))
    assert len(rows) == 11
    r = telemetry.at(rows, 3.2)
    assert r["t"] == 3.0 and r["iso"] == 100 and r["shutter"] == "1/1000.0" and r["fnum"] == 1.7
    assert r["alt"] == pytest.approx(56.0) and r["abs_alt"] == 120.5
    assert r["speed"] == pytest.approx(10.0, rel=0.02)
    assert r["vspeed"] == pytest.approx(2.0, rel=0.02)
    assert r["dist"] == pytest.approx(30.0, rel=0.02)
    s = telemetry.summary(rows)
    assert s["max_alt"] == pytest.approx(60.0) and s["path_m"] == pytest.approx(50.0, rel=0.02)
    assert len(telemetry.track(rows)) == 6


def test_parse_older_dji_format():
    old = ("1\n00:00:01,000 --> 00:00:02,000\nF/2.8, SS 500, ISO 100, EV 0, GPS (145.1234, -38.5678, 15), "
           "D 12.3m, H 40.5m, H.S 3.2m/s, V.S 0.0m/s\n")
    (r,) = telemetry.parse(old)
    assert (r["lat"], r["lon"], r["alt"], r["t"]) == (-38.5678, 145.1234, 40.5, 1.0)


def _gps_photo(path, lat, lon, seed=1):
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)

    def dms(v):
        v = abs(v)
        d = int(v); m = int((v - d) * 60); s = round(((v - d) * 60 - m) * 60, 4)
        return (d, m, s)
    gps.update({1: "S" if lat < 0 else "N", 2: dms(lat), 3: "W" if lon < 0 else "E", 4: dms(lon)})
    scene(seed, 800, 600).save(path, quality=90, exif=exif)
    return path


def test_map_lists_shots_by_trip_with_flight_paths(client, env, tmp_path):
    from tests.test_api import upload
    _, _, raw = env
    pid = upload(client, _gps_photo(tmp_path / "DJI_0001.JPG", -38.5, 145.25))["id"]
    scene(2, 400, 300).save(tmp_path / "nogps.jpg")
    upload(client, tmp_path / "nogps.jpg")
    trip = raw / "2026-04-03_Cowes"
    trip.mkdir()
    clip = make_video(trip / "DJI_0002.MP4", size="320x180", seconds=1, audio=False)
    (trip / "DJI_0002.SRT").write_text(mini3_srt())
    vid = client.app_state.lib.add_linked(clip)
    data = client.get("/api/map").json()
    by_id = {s["id"]: s for s in data["shots"]}
    assert set(by_id) == {pid, vid["id"]}                     # the photo without GPS isn't shown
    assert by_id[pid]["lat"] == pytest.approx(-38.5, abs=1e-4) and by_id[pid]["lon"] == pytest.approx(145.25, abs=1e-4)
    assert by_id[vid["id"]]["trip"].endswith("2026-04-03_Cowes")
    assert len(data["tracks"][vid["id"]]) == 6
    assert "gps" in client.app_state.lib.get(pid)                # looked up once, then stored


def test_status_reports_health(client):
    s = client.get("/api/status").json()
    assert s["version"] and s["outputs"][0]["name"] == "NAS" and s["outputs"][0]["available"]
    assert s["inputs"][0]["name"] == "Footage"
    assert set(s["jobs"]) == {"queued", "running", "done", "error"}
    assert s["library"] == {"photos": 0, "videos": 0, "rejected": 0}
    assert isinstance(s["problems"], list) and s["ok"] == (not s["problems"])
    assert "ffmpeg" in s and "gpu" in s


class Catcher:
    """A tiny local HTTP server standing in for ntfy and a webhook."""

    def __init__(self):
        self.requests = []
        catcher = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                catcher.requests.append({"method": self.command, "path": self.path,
                                         "headers": dict(self.headers), "body": body})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            do_PUT = do_POST = _do

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def catcher():
    c = Catcher()
    yield c
    c.close()


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def ehlo(self):
        pass

    def has_extn(self, name):
        return False

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


def test_notify_sends_to_every_channel(catcher, monkeypatch):
    monkeypatch.setenv("SIRIS_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SIRIS_SMTP_USER", "me")
    monkeypatch.setenv("SIRIS_SMTP_PASSWORD", "secret")
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    n = {"enabled": True, "ntfy_url": catcher.url + "/topic", "webhook_url": catcher.url + "/hook",
         "email_to": "you@example.com"}
    img = [("a.jpg", b"\xff\xd8fakejpeg")]
    res = notify.send(n, "Cowes: done", "Exported 3 photos – all good", img, {"photos": 3})
    assert res == {"ntfy": "sent", "webhook": "sent", "email": "sent"}
    ntfy = next(r for r in catcher.requests if r["path"] == "/topic")
    assert ntfy["method"] == "PUT" and ntfy["body"] == b"\xff\xd8fakejpeg" and ntfy["headers"]["Filename"] == "a.jpg"
    assert ntfy["headers"]["Title"] == "Cowes: done" and ntfy["headers"]["Message"].startswith("=?UTF-8?B?")
    hook = json.loads(next(r for r in catcher.requests if r["path"] == "/hook")["body"])
    assert hook["summary"] == {"photos": 3} and "Cowes: done" in hook["content"]
    (mail,) = FakeSMTP.sent
    assert mail["To"] == "you@example.com" and mail["Subject"] == "Cowes: done"
    assert any(p.get_content_type() == "image/jpeg" for p in mail.walk())


def test_notify_settings_and_test_endpoint(client, catcher):
    r = client.put("/api/notify", json={"enabled": True, "ntfy_url": catcher.url + "/t", "email_to": "nope"})
    assert r.json()["settings"]["email_to"] == "" and r.json()["channels"] == ["ntfy"]
    assert client.post("/api/notify/test").json()["results"] == {"ntfy": "sent"}
    client.put("/api/notify", json={"ntfy_url": "", "webhook_url": ""})
    assert client.post("/api/notify/test").status_code == 400


def _shot(path, img, when):
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = when.strftime("%Y:%m:%d %H:%M:%S")
    img.save(path, quality=95, exif=exif)


def test_watch_folder_notifies_when_a_trip_is_done(client, env, catcher):
    _, out, raw = env
    trip = raw / "Inbox" / "2026-04-03_Cowes"
    trip.mkdir(parents=True)
    w = client.app_state.watcher
    w.min_age = 0
    client.put("/api/notify", json={"enabled": True, "webhook_url": catcher.url + "/hook"})
    client.put("/api/watch", json={"enabled": True, "root": "Footage", "folder": "Inbox", "recursive": True,
                                   "out_root": "NAS", "out_folder": "Auto", "interval": 3600,
                                   "auto_level": False, "auto_exposure": False})
    when = dt.datetime(2026, 4, 3, 10, 0, 0)
    burst = scene(42)
    _shot(trip / "DJI_0010.JPG", jitter(burst, 1), when)
    _shot(trip / "DJI_0011.JPG", shaken(burst, 6), when + dt.timedelta(seconds=1))
    _shot(trip / "DJI_0012.JPG", scene(5), when + dt.timedelta(seconds=60))
    w.scan()
    w.scan()                          # processed: exports queued, trip not quiet yet
    assert not catcher.requests
    client.app_state.jobs.wait_idle(300)
    w.scan()                          # quiet and exported: one notification
    w.scan()                          # ...and only one
    (req,) = catcher.requests
    hook = json.loads(req["body"])
    assert hook["title"] == "2026-04-03_Cowes: done"
    assert "Exported 2 photos." in hook["message"] and "1 skipped as blurry or duplicate" in hook["message"]
    assert hook["summary"]["photos"] == 2 and hook["summary"]["rejected"] == 1
    assert client.get("/api/notify").json()["recent"][0]["results"] == {"webhook": "sent"}


def test_problems_are_notified_once_an_hour(client, catcher):
    client.put("/api/notify", json={"enabled": True, "webhook_url": catcher.url + "/hook"})
    w = client.app_state.watcher
    w._alert("Watch folder problem: NAS not mounted")
    w._alert("Watch folder problem: NAS not mounted")
    assert len(catcher.requests) == 1
    assert json.loads(catcher.requests[0]["body"])["title"] == "SirisDrone needs attention"
