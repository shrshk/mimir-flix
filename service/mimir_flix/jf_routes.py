"""Your own videos (Jellyfin) in mimir-flix: browse and search. Jellyfin plays them first (its web app on the
phone, its TV app on the TV); playing here and Kodi on the TV are the other options.

The browser never talks to Jellyfin: videos, HLS playlists and images come through /jf/..., which adds the API
key on the Air. Files Safari plays as they are (MP4/MOV with H.264/HEVC) stream unchanged; anything else goes
through Jellyfin's HLS, which converts on the Air's hardware."""

import re
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from mimir_flix.jellyfin import Jellyfin, JellyfinError, JellyfinMissing
from mimir_flix.kodi import Kodi, KodiError
from mimir_flix.tv import TvError
from mimir_flix.watchlist import Watchlist

FOLDERS = {"CollectionFolder", "Folder", "Series", "Season", "BoxSet", "UserView", "PhotoAlbum"}
PLAYABLE = {"Movie", "Episode", "Video", "MusicVideo"}
DIRECT_CONTAINERS = {"mp4", "m4v", "mov"}
DIRECT_VIDEO = {"h264", "hevc"}
DIRECT_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "flac", None}
# As Jellyfin's own players: under 5% watched keeps no position, from 90% on it counts as played.
MIN_RESUME, MAX_RESUME = 0.05, 0.9
ID = r"^[0-9a-f]{32}$"
# What /jf/ passes to Jellyfin: one video's stream, playlists and HLS segments, or one item's image. Every part is a
# plain name (never "." or ".."): the path arrives decoded, and the client would resolve dots to any other API.
PART = r"[A-Za-z0-9_-][A-Za-z0-9._-]*"
PASSABLE = re.compile(rf"Videos/[0-9a-f]{{32}}(/{PART})+|Items/[0-9a-f]{{32}}/Images/{PART}(/[0-9]+)?")
PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "cache-control", "last-modified")


class Play(BaseModel):
    id: str = Field(pattern=ID)


class Watching(BaseModel):
    """What Play here is playing: sent at the start, about every 10 s and on pause, and when it stops."""

    id: str = Field(pattern=ID)
    session: str = Field(pattern=r"^[0-9a-f]{32}$")
    event: Literal["start", "progress", "stopped"]
    position: float = Field(ge=0)  # seconds
    duration: float = Field(default=0, ge=0)  # seconds, for an item Jellyfin knows no length of
    paused: bool = False


def card(i: dict) -> dict:
    kind = "folder" if i.get("Type") in FOLDERS or i.get("IsFolder") else "video"
    sub = " · ".join(str(x) for x in (i.get("SeriesName"), i.get("ProductionYear")) if x)
    if i.get("Type") == "Episode" and i.get("IndexNumber"):
        sub = " · ".join(
            filter(None, [i.get("SeriesName"), f"S{i.get('ParentIndexNumber') or 0:02d}E{i['IndexNumber']:02d}"])
        )
    if i.get("RunTimeTicks") and kind == "video":
        sub = " · ".join(filter(None, [sub, f"{round(i['RunTimeTicks'] / 6e8)} min"]))
    if i.get("Type") == "TvChannel":  # a live channel (IPTV): no length, no year
        sub = "Live TV"
    pos = (i.get("UserData") or {}).get("PlaybackPositionTicks") or 0
    return {
        "title": i.get("Name", ""),
        "node": f"jf:{i['Id']}",
        "id": i["Id"],
        "kind": kind,
        "type": i.get("Type"),
        "sub": sub,
        "desc": i.get("Overview") or "",
        "thumb": f"/jf/Items/{i['Id']}/Images/Primary?maxHeight=480&quality=85"
        if i.get("ImageTags", {}).get("Primary")
        else f"/jf/Items/{i['SeriesId']}/Images/Primary?maxHeight=480&quality=85"  # an episode without its own
        if i.get("SeriesId") and i.get("SeriesPrimaryImageTag")
        else "",
        "progress": round(100 * pos / i["RunTimeTicks"]) if pos and i.get("RunTimeTicks") else 0,
        # An episode's show, for its sheet's "Go to show" (Next up, Continue watching, search)
        "show": f"jf:{i['SeriesId']}" if i.get("SeriesId") else "",
        "show_title": i.get("SeriesName") or "",
    }


def play_source(item: dict) -> str:
    src = (item.get("MediaSources") or [{}])[0]
    streams = src.get("MediaStreams") or []
    video = next((s.get("Codec") for s in streams if s.get("Type") == "Video"), None)
    audio = next((s.get("Codec") for s in streams if s.get("Type") == "Audio"), None)
    containers = set((src.get("Container") or "").split(","))
    sid = src.get("Id", item["Id"])
    if containers & DIRECT_CONTAINERS and video in DIRECT_VIDEO and audio in DIRECT_AUDIO:
        return f"/jf/Videos/{item['Id']}/stream?static=true&MediaSourceId={sid}"
    return (
        f"/jf/Videos/{item['Id']}/master.m3u8?MediaSourceId={sid}&VideoCodec=h264&AudioCodec=aac"
        "&TranscodingMaxAudioChannels=2&SegmentContainer=mp4&MinSegments=1&BreakOnNonKeyFrames=True"
        "&VideoBitrate=8000000&AudioBitrate=192000"
    )


def router(
    jf: Jellyfin,
    kodi: Kodi | None,
    tv_base: str,
    open_app=None,
    watchlist: Watchlist | None = None,
    hidden: Callable[[], set[str]] = set,
) -> APIRouter:
    """hidden: the providers' own libraries, shown in each provider's section instead of Your library."""
    api = APIRouter()

    def guard(fn, *args):
        try:
            return fn(*args)
        except (JellyfinError, TvError) as e:
            raise HTTPException(502, str(e)) from e

    @api.get("/api/jf/browse")
    def browse(id: str = ""):
        if not id:
            skip = hidden()
            views = [v for v in guard(jf.views) if v.get("Name") not in skip and v.get("CollectionType") != "playlists"]
            return {"title": "Your library", "items": [card(v) | {"kind": "folder"} for v in views]}
        parent = guard(jf.item, id)
        return {
            "title": parent.get("Name", ""),
            "desc": parent.get("Overview") or "",
            "items": [card(i) for i in guard(jf.children, id)],
        }

    @api.get("/api/jf/search")
    def search(q: str):
        # TvChannel: live channels from an IPTV provider (M2), played by Jellyfin's Live TV
        return [card(i) for i in guard(jf.search, q) if i.get("Type") in PLAYABLE | {"Series", "Folder", "TvChannel"}]

    @api.get("/api/jf/title")
    def title_page(id: str = Query(pattern=ID)):
        """A movie's page: its details, and the library's titles like it (empty when Jellyfin cannot say)."""
        i = guard(jf.details, id)
        try:
            more = [card(x) for x in jf.similar(id) if x.get("Type") in PLAYABLE | {"Series"}]
        except JellyfinError:
            more = []
        people = i.get("People") or []
        return card(i) | {
            "year": i.get("ProductionYear") or "",
            "minutes": round(i["RunTimeTicks"] / 6e8) if i.get("RunTimeTicks") else 0,
            "rating": i.get("OfficialRating") or "",
            "score": round(i["CommunityRating"], 1) if i.get("CommunityRating") else "",
            "genres": i.get("Genres") or [],
            "cast": [p["Name"] for p in people if p.get("Type") == "Actor"][:8],
            "directors": [p["Name"] for p in people if p.get("Type") == "Director"],
            "backdrop": f"/jf/Items/{id}/Images/Backdrop?maxWidth=1280&quality=85"
            if i.get("BackdropImageTags")
            else "",
            "more": more,
        }

    @api.get("/api/jf/home")
    def home_rows():
        """Home's own rows from Jellyfin: Continue watching, Next up, New episodes (a row that fails is empty)."""

        def rows(fn, *args):
            try:
                return [card(i) for i in fn(*args)]
            except JellyfinError:
                return []

        return {"resume": rows(jf.resume), "next_up": rows(jf.next_up), "new": rows(jf.recent_episodes, 7)}

    @api.get("/api/jf/now")
    def now_playing():
        """What the Jellyfin TV app is playing ({} when nothing, or the app is closed)."""
        session = guard(jf.tv_session)
        item = (session or {}).get("NowPlayingItem")
        if not item:
            return {}
        state = session.get("PlayState") or {}
        ep = (
            f"S{item.get('ParentIndexNumber') or 0:02d}E{item.get('IndexNumber') or 0:02d}"
            if item.get("IndexNumber")
            else ""
        )
        return {
            "title": item.get("Name", ""),
            "show": item.get("SeriesName", ""),
            "episode": ep,
            "paused": bool(state.get("IsPaused")),
            "minute": round((state.get("PositionTicks") or 0) / 6e8),
            "of_minutes": round((item.get("RunTimeTicks") or 0) / 6e8),
            "jellyfin_id": item.get("Id", ""),
        }

    @api.get("/api/jf/new")
    def new_episodes(days: int = 7):
        """Episodes Jellyfin added in the last few days, newest first."""
        return [
            {
                "id": i["Id"],
                "show": i.get("SeriesName", ""),
                "episode": f"S{i.get('ParentIndexNumber') or 0:02d}E{i.get('IndexNumber') or 0:02d}",
                "title": i.get("Name", ""),
                "added": (i.get("DateCreated") or "")[:10],
            }
            for i in guard(jf.recent_episodes, max(1, min(days, 30)))
        ]

    @api.get("/api/jf/play/{id}")
    def play(id: str):
        """Play here's video source: a link the page sets at once, inside the tap (iOS starts a video only from a
        tap, so the page cannot wait for an answer first); this sends it on to the file or to Jellyfin's HLS."""
        item = guard(jf.item, id)
        if item.get("Type") not in PLAYABLE:
            raise HTTPException(400, "not a video")
        return RedirectResponse(play_source(item), 302)

    @api.get("/api/jf/app")
    def app_ids():
        return guard(jf.app_ids)

    @api.get("/api/jf/resume/{id}")
    def resume_at(id: str = Path(pattern=ID)):
        """Where Play here starts: where it was left, on any player (seconds)."""
        return {"start": (guard(jf.user_data, id).get("PlaybackPositionTicks") or 0) / 1e7}

    @api.post("/api/jf/playing", status_code=204)
    def watching(w: Watching):
        """Play here's progress, into Jellyfin: Continue watching and played follow it everywhere, the TV too."""
        ticks = int(w.position * 1e7)
        event = {"start": "", "progress": "Progress", "stopped": "Stopped"}[w.event]
        body = {"ItemId": w.id, "PlaySessionId": w.session, "PositionTicks": ticks, "IsPaused": w.paused}
        try:
            jf.report(event, body | {"CanSeek": True} if w.event == "start" else body)
        except JellyfinError:
            pass  # only the "now playing" line in Jellyfin's dashboard; the user's data below is what counts
        if w.event == "start":
            return
        length = guard(jf.item, w.id).get("RunTimeTicks") or int(w.duration * 1e7)
        done = ticks / length if length else 0
        if w.event == "stopped" and done >= MAX_RESUME:
            guard(jf.mark_played, w.id)
        elif done >= MIN_RESUME or (not length and ticks):
            guard(jf.save_position, w.id, ticks)

    @api.get("/api/jf/next/{id}")
    def next_episode(id: str):
        """The episode Play here goes on to at the end ({} after the last one)."""
        n = guard(jf.next_episode, guard(jf.item, id))
        return card(n) if n else {}

    # My list's titles in the home library are Jellyfin favourites: one list for every Jellyfin app, and the
    # recommendations read the same favourites (taste.py). Entries kept here before move there once.
    def move_old_list() -> None:
        for old in [i for i in watchlist.items() if i["kind"] == "jellyfin"] if watchlist else []:
            item_id = old["url"].removeprefix("jf:")
            try:
                jf.item(item_id)
                jf.set_favourite(item_id, True)
            except JellyfinMissing:
                pass  # gone from the library: dropped
            except JellyfinError:
                return  # Jellyfin is down: the next visit tries again
            watchlist.remove(old["url"])

    @api.get("/api/jf/list")
    def listed():
        move_old_list()
        return [card(i) for i in guard(jf.favourites)]

    @api.post("/api/jf/list", status_code=201)
    def add_to_list(body: Play):
        found = card(guard(jf.item, body.id))
        if guard(jf.user_data, body.id).get("IsFavorite"):
            return {"added": [], "already": [found["title"]]}
        guard(jf.set_favourite, body.id, True)
        return {"added": [found["title"]], "already": []}

    @api.delete("/api/jf/list", status_code=204)
    def remove_from_list(id: str = Query(pattern=ID)):
        if not guard(jf.user_data, id).get("IsFavorite"):
            raise HTTPException(404, "not on My list")
        guard(jf.set_favourite, id, False)

    @api.post("/api/jf/tv")
    def on_tv(p: Play):
        """Starts it in the Jellyfin TV app."""
        item = guard(jf.item, p.id)
        if item.get("Type") not in PLAYABLE:
            raise HTTPException(400, "not a video")
        guard(jf.play_on_tv, p.id, open_app)
        return {"playing": item.get("Name")}

    @api.post("/api/jf/phone")
    def on_phone(p: Play):
        """▶ Jellyfin on the iPhone: the page opens Swiftfin at the item, and this starts it playing there."""
        item = guard(jf.item, p.id)
        if item.get("Type") not in PLAYABLE:
            raise HTTPException(400, "not a video")
        guard(jf.play_on_phone, p.id)
        return {"playing": item.get("Name")}

    @api.post("/api/jf/kodi")
    def on_kodi(p: Play):
        """Kodi on the TV, which fetches the file through the LAN resolver (the API key stays on the Air)."""
        if not kodi:
            raise HTTPException(501, "Kodi is not set up")
        item = guard(jf.item, p.id)
        if item.get("Type") not in PLAYABLE:
            raise HTTPException(400, "not a video")
        try:
            kodi.rpc("Player.Open", {"item": {"file": f"{tv_base}/jf/{p.id}"}})
        except KodiError as e:
            raise HTTPException(502, str(e)) from e
        return {"playing": item.get("Name")}

    @api.get("/jf/{path:path}")
    def passthrough(path: str, request: Request):
        """Video bytes, HLS playlists and segments, and images, with the API key added here."""
        if not PASSABLE.fullmatch(path):
            raise HTTPException(404)
        headers = {k: v for k, v in request.headers.items() if k.lower() in ("range", "if-range")}
        r = guard(jf.raw, "/" + path, dict(request.query_params), headers)
        out = {k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS}
        return StreamingResponse(
            r.iter_bytes(1 << 16), status_code=r.status_code, headers=out, background=BackgroundTask(r.close)
        )

    return api
