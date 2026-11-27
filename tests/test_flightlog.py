"""1.77: the flight log, worked out from clips' DJI .SRT."""
from app import flightlog
from tests.conftest import make_video
from tests.test_trips import mini3_srt


def _clip(env, client, name, stamp, seconds=5):
    _, _, raw = env
    trip = raw / "2026-04-03_Cowes"
    trip.mkdir(exist_ok=True)
    clip = make_video(trip / f"{name}.MP4", size="320x180", seconds=1, audio=False)
    (trip / f"{name}.SRT").write_text(mini3_srt(seconds=seconds).replace("2026-04-03 10:12:31.123", stamp))
    return client.app_state.lib.add_linked(clip)


def test_clips_close_together_are_one_flight_and_a_long_gap_starts_another(client, env):
    a = _clip(env, client, "DJI_0001", "2026-04-03 10:12:31.123")
    b = _clip(env, client, "DJI_0002", "2026-04-03 10:15:00.000")          # 2.5 min later: the same flight
    c = _clip(env, client, "DJI_0003", "2026-04-03 10:40:00.000", seconds=8)  # 25 min later: a new battery
    data = client.get("/api/flights").json()
    assert [f["clips"] for f in data["flights"]] == [1, 2]                 # newest first
    first = data["flights"][1]
    assert first["id"] == "20260403-101231" and set(first["items"]) == {a["id"], b["id"]}
    assert first["max_alt_m"] == 60.0 and first["max_speed_kmh"] == 36.0 and first["furthest_m"] == 50
    assert data["flights"][0]["items"] == [c["id"]] and data["flights"][0]["max_alt_m"] == 66.0
    t = data["totals"]
    assert t["flights"] == 2 and t["highest_m"] == 66.0 and t["furthest_m"] == 80 and t["last"]["id"] == "20260403-104000"


def test_a_note_stays_with_its_flight_and_the_summary_is_small(client, env):
    _clip(env, client, "DJI_0010", "2026-04-03 10:12:31.123")
    fid = client.get("/api/flights").json()["flights"][0]["id"]
    assert client.put(f"/api/flights/{fid}/note", json={"note": "Battery 2, gusty"}).json() == {"ok": True}
    assert client.get("/api/flights").json()["flights"][0]["note"] == "Battery 2, gusty"
    assert client.put("/api/flights/../x/note", json={"note": "x"}).status_code == 404
    s = client.get("/api/flights/summary").json()
    assert s["flights"] == 1 and s["last"]["note"] == "Battery 2, gusty" and "items" not in s["last"] and s["version"]


def test_no_srt_no_flight(client, env):
    _, _, raw = env
    clip = make_video(raw / "DJI_0099.MP4", size="320x180", seconds=1, audio=False)
    client.app_state.lib.add_linked(clip)
    assert client.get("/api/flights").json()["flights"] == [] and flightlog.totals([])["last"] is None
