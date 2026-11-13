"""A provider plugin's pages in mimir-flix (catalog.py): browse, search and explore here; Jellyfin plays everything.
Playing a video or a show writes .strm files into the provider's Jellyfin libraries (on-demand movies, live streams,
shows), then opens it in Jellyfin or starts it in the Jellyfin TV app. Playing here in the browser and Kodi on the TV
are the other options.

Every route is under /api/p/<provider id>/. A folder is named by its node (catalog.node_id: the provider and the
folder's url); a video by its url. Each provider has three Jellyfin libraries, named after it ("<name>",
"<name> Movies", "<name> Live"), with their .strm files in <media_root>/<id>, <id>-movies and <id>-live."""

import base64
import hashlib
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from mimir_flix.catalog import Catalog, CatalogError, Catalogs, Served, node_id, node_parts
from mimir_flix.hls import PASS, HlsError, HlsRelay, from_tailnet, is_hls, is_playlist
from mimir_flix.jellyfin import Jellyfin, JellyfinError
from mimir_flix.kodi import Kodi, KodiError
from mimir_flix.strm import follow, followed, play_line, safe, show_folder, write_movie, write_show
from mimir_flix.tv import TvError
from mimir_flix.watchlist import Watchlist

THUMB = "/api/thumb?u="


def libraries(entry: dict) -> dict[str, str]:
    """A provider's Jellyfin libraries: shows, on-demand videos, live streams."""
    return {"shows": entry["name"], "movies": f"{entry['name']} Movies", "live": f"{entry['name']} Live"}


def roots(media_root: Path, pid: str) -> dict[str, Path]:
    return {"shows": media_root / pid, "movies": media_root / f"{pid}-movies", "live": media_root / f"{pid}-live"}


class Video(BaseModel):
    url: str
    title: str = "Video"
    page: str = ""  # the list it is in (node); a live channel's link is refreshed from there when it expires
    tv: bool = False  # start it in the Jellyfin TV app; otherwise just return its Jellyfin id


class Show(BaseModel):
    node: str
    title: str
    episode: str = ""  # one episode's link; the whole show when empty
    tv: bool = False


class Saved(BaseModel):
    """A title for My list: a show (node) or a video (url), as the page shows it."""

    node: str = ""
    url: str = ""
    title: str
    thumb: str = ""  # the page's thumbnail link (/api/thumb?u=...)
    sub: str = ""
    page: str = ""
    here: bool = False


def router(
    catalogs: Catalogs,
    jf: Jellyfin,
    kodi: Kodi | None,
    media_root: Path,
    resolver: str,
    thumbs: Path,
    open_app=None,
    hls: HlsRelay | None = None,
    watchlist: Watchlist | None = None,
    play_app=None,
) -> APIRouter:
    hls = hls or HlsRelay()
    watchlist = watchlist or Watchlist(thumbs.parent / "mimir-flix.db")
    api = APIRouter()
    thumbs.mkdir(parents=True, exist_ok=True)
    # Thumbnails: never from this Mac itself, redirects included (as the relay).
    http = httpx.Client(timeout=20, follow_redirects=True, event_hooks={"request": [HlsRelay.refuse_inside]})
    listed: set[str] = set()  # thumbnails a catalogue listed; the proxy fetches nothing else

    def provider(pid: str) -> tuple[dict, Catalog]:
        found = catalogs.get(pid)
        if not found:
            raise HTTPException(404, "no such provider")
        return found

    def page_url(pid: str, cat: Catalog, node: str) -> str:
        try:
            owner, url = node_parts(node)
        except ValueError as e:
            raise HTTPException(400, "bad node") from e
        if owner != pid or not cat.owns(url):
            raise HTTPException(400, "not a page of this provider")
        return url

    def thumb_link(url: str) -> str:
        if not url:
            return ""
        listed.add(url)
        return THUMB + base64.urlsafe_b64encode(url.encode()).decode()

    def only_listed(cat: Catalog, url: str) -> None:
        """A video the provider listed, never an address a caller made up (this Mac would fetch or relay it)."""
        if not cat.plays(url):
            raise HTTPException(404, "not a video of this provider")

    def guard(fn, *args):
        try:
            return fn(*args)
        except CatalogError as e:
            raise HTTPException(502, str(e)) from e

    known: dict[str, tuple[float, dict]] = {}  # Jellyfin's answer per provider, reused for a few seconds

    def in_jellyfin(entry: dict) -> dict[str, str]:
        """What Jellyfin has now of this provider, by link: its item id. Asked, not assumed, so something removed in
        Jellyfin shows as not added. Videos are matched through their .strm files, shows through their followed
        folder."""
        hit = known.get(entry["id"])
        if hit and time.monotonic() - hit[0] < 10:
            return hit[1]
        shows = {safe(title): url for title, url in followed(roots(media_root, entry["id"])["shows"]).items()}
        have = {}
        try:
            for library in libraries(entry).values():
                for it in jf.library_items(library):
                    path = Path(it.get("Path") or "")
                    if path.suffix == ".strm" and path.is_file():
                        for url in parse_qs(urlsplit(path.read_text().strip()).query).get("path", []):
                            have[url] = it["Id"]
                    elif it.get("Type") == "Series" and path.name in shows:
                        have[shows[path.name]] = it["Id"]
        except JellyfinError:
            return {}  # Jellyfin not answering: nothing is marked
        known[entry["id"]] = (time.monotonic(), have)
        return have

    def item(pid: str, i: dict, have: dict[str, str]) -> dict:
        out = {
            "p": pid,
            "title": i["title"],
            "desc": i.get("desc", ""),
            "kind": i["kind"],
            "thumb": thumb_link(i.get("thumb", "")),
            "sub": i.get("sub", ""),
            "jf_id": have.get(i["url"], ""),
        }
        if i["kind"] == "folder":
            out["node"] = node_id(pid, i["url"])
        else:
            out |= {"url": i["url"], "here": i.get("here", False)}
        return out

    @api.get("/api/p/{pid}/browse")
    def browse(pid: str, node: str = ""):
        """The sections, or one folder: items plus a breadcrumb trail."""
        entry, cat = provider(pid)
        if not node:
            sections = [
                {"p": pid, "title": s["name"], "kind": "folder", "node": node_id(pid, s["url"])}
                | {"thumb": "", "desc": "", "sub": ""}
                for s in guard(cat.sections)
            ]
            return {"title": entry["name"], "trail": [], "items": sections}
        url = page_url(pid, cat, node)
        have = in_jellyfin(entry)
        items = [item(pid, i, have) for i in guard(cat.page, url) if i["kind"] in ("folder", "video")]
        watchlist.seen(url)
        trail = cat.trail(url)
        return {
            "title": trail[-1] if trail else entry["name"],
            "trail": trail,
            "items": items,
            "jf_id": have.get(url, ""),
        }

    @api.get("/api/p/{pid}/home")
    def home(pid: str, lang: str = ""):
        """Home's rows (a row that fails is left out)."""
        entry, cat = provider(pid)
        have = in_jellyfin(entry)
        rows = guard(cat.home, lang)
        return [
            {"section": r["section"], "node": node_id(pid, r["url"]), "items": [item(pid, i, have) for i in r["items"]]}
            for r in rows
            if r["items"]
        ]

    @api.get("/api/p/{pid}/search")
    def search(pid: str, q: str, lang: str = ""):
        entry, cat = provider(pid)
        have = in_jellyfin(entry)
        return [
            {"section": g["section"], "items": [item(pid, i, have) for i in g["items"]]}
            for g in guard(cat.search, q, lang)
        ]

    @api.get("/api/p/{pid}/live")
    def live(pid: str, q: str):
        """The live streams showing a match or channel now, best first; page is the list to refresh an expired link
        from, as for any live video."""
        _, cat = provider(pid)
        return [s | {"p": pid, "page": node_id(pid, s["page"])} for s in guard(cat.live, q)]

    @api.get("/api/p/{pid}/suggest")
    def suggest(pid: str, q: str):
        """Titles for search suggestions as you type (the provider's local index; nothing is fetched)."""
        if len(q.strip()) < 2:
            return []
        return list(dict.fromkeys(provider(pid)[1].suggest(q)))[:5]

    @api.get("/api/p/{pid}/status")
    def status(pid: str):
        return {"note": provider(pid)[1].status()}

    @api.get("/api/thumb")
    def thumb(u: str):
        """Thumbnails are often plain http on the provider's side; served from here so an https page can show them."""
        try:
            url = base64.urlsafe_b64decode(u.encode()).decode()
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(404, "unknown thumbnail") from e
        f = thumbs / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".jpg")
        # Only what a catalogue listed (since this start), what is already cached (listed before a restart), or a
        # host a catalogue vouches for: never an open proxy.
        if url not in listed and not f.exists() and not any(cat.thumb_ok(url) for _, cat in catalogs.all()):
            raise HTTPException(404, "unknown thumbnail")
        if not f.exists():
            try:
                r = http.get(url)
            except httpx.HTTPError as e:
                raise HTTPException(404, "no thumbnail") from e
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                raise HTTPException(404, "no thumbnail")
            f.write_bytes(r.content)
        return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=604800"})

    @api.get("/api/p/{pid}/play")
    def play_here(pid: str, u: str, request: Request):
        """The fallback player in the browser: a 302 to a playable link. A relayed stream, or a player away from home
        (a Tailscale address), gets it through this Mac instead, as the resolver does."""
        if not u.startswith("https://"):
            raise HTTPException(400, "not a playable link")
        _, cat = provider(pid)
        only_listed(cat, u)
        link = guard(cat.link, u)
        away = from_tailnet(request.client.host if request.client else "", request.headers.get("x-forwarded-for"))
        if cat.relayed(u) or (away and is_hls(link)):  # plain http (an https page will not play it), or away
            link = "/api/relay?u=" + quote(hls.allow(link), safe="") + "&x=.m3u8"
        elif away:
            return relay(hls.allow(link), request)
        return RedirectResponse(link, status_code=302)

    @api.get("/api/relay")
    def relay(u: str, request: Request):
        ask = {k: v for k, v in request.headers.items() if k.lower() in ("range", "if-range")}
        try:
            r = hls.open(u, ask)
        except HlsError as e:
            raise HTTPException(404 if "handed out" in str(e) else 502, str(e)) from e
        if is_playlist(r):
            try:
                body = hls.rewrite(r.read().decode(errors="replace"), str(r.url), "/api/relay?u=")
            finally:
                r.close()
            return Response(
                body, r.status_code, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache"}
            )
        out = {k: v for k, v in r.headers.items() if k.lower() in PASS}
        return StreamingResponse(r.iter_bytes(1 << 16), r.status_code, headers=out, background=BackgroundTask(r.close))

    @api.post("/api/p/{pid}/kodi")
    def play_on_kodi(pid: str, v: Video):
        if not kodi:
            raise HTTPException(501, "Kodi is not set up (no password file)")
        _, cat = provider(pid)
        only_listed(cat, v.url)
        try:
            kodi.play(cat.kodi(v.url, v.title) or guard(cat.link, v.url))  # the provider's own add-on, or the stream
        except KodiError as e:
            raise HTTPException(502, str(e)) from e
        return {"playing": v.title}

    def jellyfin(fn, *args):
        try:
            return fn(*args)
        except (JellyfinError, TvError) as e:
            raise HTTPException(502, str(e)) from e

    def ensure_library(name: str, kind: str, root: Path) -> None:
        lib = next((x for x in jellyfin(jf.libraries) if x["Name"] == name), None)
        if not lib:
            jellyfin(jf.create_library, name, kind, [str(root)])
        else:
            jellyfin(jf.scan, lib.get("ItemId"))

    def hand_over(pid: str, library: str, path: Path, tv: bool) -> str:
        item_id = jellyfin(jf.find, library, str(path))
        known.pop(pid, None)  # Jellyfin has something new
        if tv:
            jellyfin(lambda: jf.play_on_tv(item_id, open_app, play_app=play_app))
        return item_id

    @api.post("/api/p/{pid}/jellyfin")
    def show_in_jellyfin(pid: str, show: Show):
        """A show: its episodes as .strm files in the provider's shows library; Jellyfin's id for it (or for one
        episode)."""
        entry, cat = provider(pid)
        url = page_url(pid, cat, show.node)
        root = roots(media_root, pid)["shows"]
        folder = show_folder(root, show.title, url, cat)  # its own name if the same show from another section is added
        files = guard(write_show, cat, pid, url, folder, root, resolver)
        follow(root, folder, url)  # new episodes are added as they appear
        if show.episode and show.episode not in files:
            raise HTTPException(404, "no such episode in this show")
        ensure_library(libraries(entry)["shows"], "tvshows", root)
        path = files[show.episode] if show.episode else root / safe(folder)
        return {"id": hand_over(pid, libraries(entry)["shows"], path, show.tv), "episodes": len(files)}

    @api.post("/api/p/{pid}/jellyfin/video")
    def video_in_jellyfin(pid: str, v: Video):
        """One video: on demand (signed when played) in "<name> Movies", a live stream in "<name> Live"."""
        if not v.url.startswith("https://"):
            raise HTTPException(400, "not a playable link")
        entry, cat = provider(pid)
        only_listed(cat, v.url)  # the .strm file sends players to it through the resolver, which asks the same
        which = "movies" if cat.on_demand(v.url) else "live"
        library, root = libraries(entry)[which], roots(media_root, pid)[which]
        page = page_url(pid, cat, v.page) if v.page else ""
        target = write_movie(pid, v.url, v.title, root, resolver, page)
        ensure_library(library, "movies" if which == "movies" else "homevideos", root)
        return {"id": hand_over(pid, library, target, v.tv)}

    @api.get("/api/p/{pid}/list")
    def my_list(pid: str):
        """This provider's titles on My list, newest first, as tiles (with what Jellyfin has and how many new
        episodes). key is what DELETE takes: a show's node or a video's url."""
        entry, _ = provider(pid)
        have = in_jellyfin(entry)
        first = catalogs.default()[0]["id"]  # entries saved before providers had ids
        out = []
        for s in watchlist.items():
            if s["kind"] not in ("show", "video") or (s["provider"] or first) != pid:
                continue
            if have.get(s["url"]):  # Jellyfin has it now: a favourite there instead (My list reads those)
                try:
                    jf.set_favourite(have[s["url"]], True)
                    watchlist.remove(s["url"])
                    continue
                except JellyfinError:
                    pass  # Jellyfin is down: kept here until the next visit
            show = s["kind"] == "show"
            i = {"title": s["title"], "desc": "", "kind": "folder" if show else "video", "thumb": s["thumb"]}
            i |= {"url": s["url"], "sub": s["sub"], "here": bool(s["here"])}
            tile = item(pid, i, have) | {"key": node_id(pid, s["url"]) if show else s["url"], "new": s["new"]}
            if not show and s["page"]:
                tile["page"] = node_id(pid, s["page"])
            out.append(tile)
        return out

    @api.post("/api/p/{pid}/list", status_code=201)
    def save(pid: str, t: Saved):
        """Saves a pointer to the title; nothing is fetched, downloaded or added to Jellyfin. A title Jellyfin has
        already is a Jellyfin favourite instead, like any title in the library (no copy is kept here)."""
        entry, cat = provider(pid)
        if t.node:
            url, kind = page_url(pid, cat, t.node), "show"
        elif t.url.startswith("https://"):
            url, kind = t.url, "video"
        else:
            raise HTTPException(400, "not a show or video of this provider")
        thumb = ""
        if t.thumb.startswith(THUMB):
            try:
                thumb = base64.urlsafe_b64decode(t.thumb.removeprefix(THUMB).encode()).decode()
            except (ValueError, UnicodeDecodeError):
                thumb = ""
        if jf_id := in_jellyfin(entry).get(url):
            try:
                jf.set_favourite(jf_id, True)
            except JellyfinError as e:
                raise HTTPException(502, str(e)) from e
            return {"key": t.node or url, "jellyfin_id": jf_id}
        page = page_url(pid, cat, t.page) if t.page else ""
        watchlist.add(
            url, kind, t.title, thumb if thumb in listed else "", t.sub, page, t.here and kind == "video", pid
        )
        if kind == "show":  # what it has now, so later episodes count as new
            threading.Thread(target=watchlist.check, args=(catalogs,), kwargs={"only": url}, daemon=True).start()
        return {"key": t.node or url}

    @api.delete("/api/p/{pid}/list", status_code=204)
    def unsave(pid: str, key: str):
        _, cat = provider(pid)
        if not watchlist.remove(key if key.startswith("https://") else page_url(pid, cat, key)):
            raise HTTPException(404, "not on My list")

    return api


def hidden_libraries(catalogs: Catalogs) -> set[str]:
    """The providers' own Jellyfin libraries: shown in each provider's section, not in Your library."""
    return {name for entry, _ in catalogs.all() for name in libraries(entry).values()}


def listing(catalogs: Catalogs) -> list[dict]:
    """The providers for the page: [{"id", "name"}]."""
    return [{"id": e["id"], "name": e["name"]} for e, _ in catalogs.all()]


def plugin_paths(catalogs: Catalogs, resolver: str):
    """The providers' own paths (Catalog.serve): each plugin is asked in Settings' order; None when none answers."""

    def answer(path: str, request: Request):
        base = f"{request.url.scheme}://{request.url.netloc}"
        for entry, cat in catalogs.all():
            ctx = Served(lambda url, page="", pid=entry["id"]: play_line(resolver, pid, url, page).strip(), base)
            found = cat.serve(path, dict(request.query_params), ctx)
            if found is not None:
                return found
        return None

    return answer
