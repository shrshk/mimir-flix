import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimir_flix.tvmessage import PHOTO_FOR, SESSION, Overlay, router


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def clients(overlay):
    app = FastAPI()
    app.include_router(router(overlay))
    phone = TestClient(app, client=("127.0.0.1", 5000), headers={"X-Forwarded-For": "100.64.0.2"})  # via Caddy
    lan = TestClient(app, client=("127.0.0.1", 5000), headers={"X-Forwarded-For": "192.0.2.30"})
    return phone, lan


def test_only_tailnet_devices_can_send_and_the_tv_gets_what_was_sent():
    overlay = Overlay(clock=Clock())
    phone, lan = clients(overlay)
    tv = overlay.listen()
    assert lan.post("/api/tv/send", json={"message": {"kind": "clear"}}).status_code == 403
    assert phone.post("/api/tv/send", json={"message": {"kind": "clear"}}).status_code == 202
    # Notes are gone (user: doodle and photo are enough)
    assert phone.post("/api/tv/send", json={"message": {"kind": "note", "text": "hi"}}).status_code == 422
    stroke = {
        "kind": "stroke",
        "id": 0,
        "color": "#ffd60a",
        "width": 0.012,
        "points": [[0.1, 0.2, 0.5], [0.3, 0.4, 0.6]],
    }
    assert phone.post("/api/tv/send", json={"message": stroke}).status_code == 202
    assert [tv.get_nowait() for _ in range(2)] == [{"kind": "clear"}, stroke]
    assert tv.empty()  # the LAN's message never reached it
    bad = stroke | {"points": [[0.1, 0.2]]}
    assert phone.post("/api/tv/send", json={"message": bad}).status_code == 422
    assert phone.get("/send").status_code == 200


def test_a_tv_that_connects_late_gets_the_session_but_not_an_old_one():
    clock = Clock()
    overlay = Overlay(clock=clock)
    overlay.publish({"kind": "clear", "session": "old"})
    clock.t += SESSION + 1
    overlay.publish({"kind": "clear"})
    late = overlay.listen()
    assert late.get_nowait() == {"kind": "clear"} and late.empty()
    clock.t += SESSION + 1
    assert overlay.listen().empty()  # quiet for a while: nothing to replay


def test_the_tv_is_woken_once_while_nobody_listens():
    clock, woken = Clock(), []
    done = threading.Event()
    overlay = Overlay(wake=lambda: (woken.append(1), done.set()), clock=clock)
    for _ in range(5):
        overlay.publish({"kind": "clear"})
    assert done.wait(2) and woken == [1]  # a burst of strokes starts the TV's overlay once
    q = overlay.listen()
    clock.t += 30
    overlay.publish({"kind": "clear"})
    assert woken == [1]  # someone is listening: no ADB
    overlay.leave(q)


def test_a_photo_is_fetched_once_and_never_kept():
    clock = Clock()
    overlay = Overlay(clock=clock)
    phone, lan = clients(overlay)
    tv = overlay.listen()
    assert lan.post("/api/tv/photo", content=b"jpg", headers={"Content-Type": "image/jpeg"}).status_code == 403
    assert phone.post("/api/tv/photo", content=b"x", headers={"Content-Type": "text/html"}).status_code == 415
    assert (
        phone.post("/api/tv/photo", content=b"\xff\xd8jpg", headers={"Content-Type": "image/jpeg"}).status_code == 202
    )
    url = tv.get_nowait()["url"]
    first = lan.get(url)
    assert first.content == b"\xff\xd8jpg" and first.headers["content-type"] == "image/jpeg"
    assert lan.get(url).status_code == 404  # gone once the TV has it
    phone.post("/api/tv/photo", content=b"late", headers={"Content-Type": "image/png"})
    clock.t += PHOTO_FOR
    assert lan.get(tv.get_nowait()["url"]).status_code == 404  # nobody fetched it in time


def test_the_stream_sends_events_and_lets_go_of_the_listener():
    overlay = Overlay(clock=Clock())
    q = overlay.listen()
    q.put({"kind": "clear"})
    stream = overlay.stream(q)
    assert next(stream) == ": hello\n\n"
    assert json.loads(next(stream).removeprefix("data: ")) == {"kind": "clear"}
    stream.close()
    assert overlay.listeners == []
