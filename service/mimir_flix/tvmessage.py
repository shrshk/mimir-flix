"""Messages to the TV (I4): a live doodle or a photo from the phone shows over whatever is on the TV for
a few seconds, then lets go. Nothing is kept: strokes only pass through memory, a photo is held until the
TV fetched it (a minute at most), and none of it is written to disk or logged.

The phone (a tailnet device; nobody else may send) posts here. The TV's overlay, part of the mimir-flix TV app, is
started over ADB when a message comes and stops by itself after a quiet spell, so it costs the TV nothing at rest;
it reads the events as a stream (server-sent events) over the LAN. A TV that connects a moment late gets the
current session's events first, so the start of a doodle is not lost.
"""

import json
import queue
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from mimir_flix.hls import from_tailnet

SESSION = 20  # seconds of quiet that end a session (its replay is dropped)
REPLAY = 2000  # events kept for a late TV
WAKE_EVERY = 8  # seconds between attempts to start the TV's overlay while none is listening
PHOTO_FOR = 60  # seconds a photo waits for the TV
PHOTO_MAX = 6 * 1024 * 1024
PING = 10  # seconds between keep-alives on the stream
SEND_PAGE = Path(__file__).parent / "static" / "send.html"


class Stroke(BaseModel):
    """A piece of a stroke: points (x, y in 0..1 of the drawing box, pressure 0..1) to add to stroke `id`."""

    kind: Literal["stroke"]
    id: int = Field(ge=0)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    width: float = Field(gt=0, le=0.05)  # of the box's width
    points: list[Annotated[list[float], Field(min_length=3, max_length=3)]] = Field(min_length=1, max_length=200)
    # Set on the phone's send page; left out by an older page, then the TV uses 16:9 and a few seconds.
    aspect: float | None = Field(None, ge=0.2, le=5)  # the drawing box's width / height; the TV fits it, centred
    hold: int | None = Field(None, ge=0, le=3600)  # seconds the TV keeps it after the last stroke; 0: until cleared


class Clear(BaseModel):
    kind: Literal["clear"]


Message = Annotated[Stroke | Clear, Field(discriminator="kind")]


class Send(BaseModel):
    message: Message


class Overlay:
    def __init__(self, wake: Callable[[], None] | None = None, clock: Callable[[], float] = time.monotonic):
        self.wake, self.clock = wake, clock
        self.lock = threading.Lock()
        self.listeners: list[queue.Queue] = []
        self.replay: list[dict] = []
        self.last = self.woken = -1e9
        self.photos: dict[str, tuple[float, bytes, str]] = {}

    def publish(self, event: dict) -> None:
        with self.lock:
            now = self.clock()
            if now - self.last > SESSION:
                self.replay = []
            self.last = now
            self.replay = (self.replay + [event])[-REPLAY:]
            listeners = list(self.listeners)
            wake = not listeners and self.wake and now - self.woken > WAKE_EVERY
            if wake:
                self.woken = now
        for q in listeners:
            q.put(event)
        if wake:
            threading.Thread(target=self.wake, daemon=True).start()  # ADB takes a second; the phone does not wait

    def listen(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            if self.clock() - self.last <= SESSION:
                for e in self.replay:
                    q.put(e)
            self.listeners.append(q)
        return q

    def leave(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def add_photo(self, data: bytes, media_type: str) -> str:
        pid = secrets.token_urlsafe(12)
        with self.lock:
            now = self.clock()
            self.photos = {k: v for k, v in self.photos.items() if now - v[0] < PHOTO_FOR}
            self.photos[pid] = (now, data, media_type)
        return pid

    def take_photo(self, pid: str) -> tuple[bytes, str] | None:
        """The photo, once: the TV fetches it, then it is gone."""
        with self.lock:
            hit = self.photos.pop(pid, None)
        if not hit or self.clock() - hit[0] >= PHOTO_FOR:
            return None
        return hit[1], hit[2]

    def stream(self, q: queue.Queue) -> Iterator[str]:
        try:
            yield ": hello\n\n"
            while True:
                try:
                    yield f"data: {json.dumps(q.get(timeout=PING))}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            self.leave(q)


def router(overlay: Overlay) -> APIRouter:
    api = APIRouter(prefix="/api/tv")
    page = APIRouter()

    @page.get("/send")
    def send_page():
        """The phone's page: draw, or a photo, to the TV."""
        return FileResponse(SEND_PAGE, headers={"Cache-Control": "no-cache"})

    def from_phone(request: Request) -> None:
        if not from_tailnet(request.client.host if request.client else "", request.headers.get("x-forwarded-for")):
            raise HTTPException(403, "only your devices on the tailnet can send to the TV")

    @api.post("/send", status_code=202)
    def send(body: Send, request: Request):
        from_phone(request)
        overlay.publish(body.message.model_dump(exclude_none=True))

    @api.post("/photo", status_code=202)
    async def photo(request: Request):
        """The photo itself as the body (the phone sends it scaled down, as JPEG)."""
        from_phone(request)
        media_type = request.headers.get("content-type", "")
        if media_type not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(415, "a JPEG, PNG or WebP image")
        data = await request.body()
        if not data or len(data) > PHOTO_MAX:
            raise HTTPException(413, "up to 6 MB")
        overlay.publish({"kind": "photo", "url": f"/api/tv/photo/{overlay.add_photo(data, media_type)}"})

    @api.get("/photo/{pid}")
    def get_photo(pid: str):
        hit = overlay.take_photo(pid)
        if hit is None:
            raise HTTPException(404, "gone")
        return Response(hit[0], media_type=hit[1], headers={"Cache-Control": "no-store"})

    @api.get("/events")
    def events():
        """The TV's overlay reads this; a TV that connects a moment late gets the current session first."""
        return StreamingResponse(
            overlay.stream(overlay.listen()),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    page.include_router(api)
    return page
