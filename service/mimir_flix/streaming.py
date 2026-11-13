"""Where a title streams: TMDB's search and its "where to watch" lists (JustWatch's data), cut down to the apps the
house has. Nothing is crawled: one search per query and one lookup per title, each kept a day. The TMDB credential
(from your password manager, in a file beside Jellyfin's key) is either the "API Read Access Token", sent in a header, or the
short v3 "API Key", which TMDB only takes in the URL: it is added to the request alone, never to the cache's keys,
and request URLs are not logged.

TMDB gives no app's own title id, so a title opens on the app's search (phone) or in the app (TV); the TV app
tries the app's own search first."""

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from mimir_flix.tv import TvError

API = "https://api.themoviedb.org/3"
# The app's own link for a title (TMDB has none): JustWatch's listing, matched to the title by its TMDB id. An
# unofficial API, asked only when an app button is pressed, kept a day; without it, the app opens at its search.
JUSTWATCH = "https://apis.justwatch.com/graphql"
JUSTWATCH_QUERY = """query Find($q: String!, $country: Country!) {
  popularTitles(country: $country, first: 5, filter: {searchQuery: $q}) { edges { node {
    objectType content(country: $country, language: "en") { externalIds { tmdbId } }
    offers(country: $country, platform: WEB) { monetizationType package { clearName } standardWebURL } } } } }"""
OTHER_DOMAINS = {"prime": ("primevideo.com",)}  # links an app opens besides its web page's own domain
POSTER = "https://image.tmdb.org/t/p/w342"
BACKDROP = "https://image.tmdb.org/t/p/w1280"  # wide, for Home's featured title
KEPT_FOR = 24 * 3600
LOOKUPS = 8  # the first 8 movies and shows of a search are looked up; the rest are rarely what was meant
TRENDING = 20  # Home's row: the week's top 20, of which those on the house's apps show
CATEGORIES = ("flatrate", "free", "ads")  # subscriptions and free; renting and buying are left out


@dataclass(frozen=True)
class App:
    key: str
    name: str
    package: str  # the TV app
    web: str  # on the phone: the app's search for the title ({q}), or its home page; the app opens the link
    providers: str  # TMDB provider names that play in this app
    alt: tuple[str, ...] = ()  # the same app under another package on some TVs

    @property
    def packages(self) -> str:
        """The TV app's packages, "|"-joined: the TV opens whichever is installed (Tv.open_app)."""
        return "|".join((self.package, *self.alt))


# Every app mimir-flix knows; settings' streaming_apps picks the ones a house pays for (empty: all of them).
# Packages are the Google TV apps; where an app's search link is not known, the web link is its home page.
APPS = [
    App("netflix", "Netflix", "com.netflix.ninja", "https://www.netflix.com/search?q={q}", r"^netflix"),
    App(
        "prime",
        "Prime Video",
        "com.amazon.amazonvideo.livingroom",
        "https://www.amazon.com/s?k={q}&i=instant-video",
        r"^amazon prime video|amazon channel$",
    ),  # with its channels (Paramount+, ESPN+ … bought through Prime)
    App("peacock", "Peacock", "com.peacocktv.peacockandroid", "https://www.peacocktv.com/", r"^peacock"),
    App("hulu", "Hulu", "com.hulu.livingroomplus", "https://www.hulu.com/search?q={q}", r"^hulu"),
    App("disney", "Disney+", "com.disney.disneyplus", "https://www.disneyplus.com/search?q={q}", r"^disney plus"),
    App("max", "Max", "com.wbd.stream", "https://play.max.com/", r"^(hbo )?max\b"),
    App(
        "appletv",
        "Apple TV+",
        "com.apple.atve.androidtv.appletv",
        "https://tv.apple.com/search?term={q}",
        r"^apple tv\+|^apple tv plus",
        ("com.apple.atve.sony.appletv",),  # Sony's
    ),
    App(
        "paramount",
        "Paramount+",
        "com.cbs.ott",
        "https://www.paramountplus.com/",
        r"^paramount(\+| plus)(?! amazon channel)",
    ),
    App("espn", "ESPN+", "com.espn.score_center", "https://www.espn.com/watch/", r"^espn"),
    App(
        "youtube",
        "YouTube",
        "com.google.android.youtube.tv",
        "https://www.youtube.com/results?search_query={q}",
        r"^youtube( premium)?$",
    ),  # not YouTube TV
    App("sling", "Sling TV", "com.sling", "https://watch.sling.com/", r"^sling"),
    App("tubi", "Tubi", "com.tubitv", "https://tubitv.com/search/{q}", r"^tubi"),
    App("pluto", "Pluto TV", "tv.pluto.android", "https://pluto.tv/", r"^pluto"),
]


def domain(url: str) -> str:
    return ".".join(urlsplit(url).netloc.lower().split(".")[-2:])


def own_link(app: App, url: str) -> bool:
    """An https link on the app's own domain: the only kind handed to the TV (and to the phone's redirect)."""
    return urlsplit(url).scheme == "https" and domain(url) in (domain(app.web), *OTHER_DOMAINS.get(app.key, ()))


class StreamingError(Exception):
    pass


def apps_for(providers: dict, apps: list[App]) -> list[App]:
    """The house's apps among one country's providers of a title, in the house's order."""
    names = {p["provider_name"].strip().lower() for c in CATEGORIES for p in providers.get(c, [])}
    return [a for a in apps if any(re.search(a.providers, n) for n in names)]


def pick(keys: str) -> list[App]:
    """The apps named in streaming_apps ("netflix,peacock"), in that order; empty: every app."""
    wanted = [k.strip() for k in keys.split(",") if k.strip()]
    unknown = set(wanted) - {a.key for a in APPS}
    if unknown:
        raise SystemExit(f"streaming_apps: unknown apps {sorted(unknown)}; known: {', '.join(a.key for a in APPS)}")
    return [next(a for a in APPS if a.key == k) for k in wanted] or APPS


def link(app: App, title: str) -> dict:
    return {"app": app.key, "name": app.name, "package": app.package, "web": app.web.format(q=quote(title))}


class Streaming:
    def __init__(
        self,
        token: str,
        db: Path,
        apps: list[App] = APPS,
        region: str = "US",
        transport: httpx.BaseTransport | None = None,
    ):
        self.token = token
        self.apps = apps
        self.region = region
        # A Read Access Token is a JWT ("eyJ…"); anything else is a v3 API key
        self.auth = {"headers": {"Authorization": f"Bearer {token}"}} if token.startswith("eyJ") else {}
        self.key_param = {} if self.auth else {"api_key": token}
        self.http = httpx.Client(base_url=API, timeout=15, transport=transport, **self.auth)
        self.jw = httpx.Client(timeout=15, transport=transport)  # no TMDB credential on this one
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS tmdb (k TEXT PRIMARY KEY, fetched REAL, body TEXT)")

    @property
    def ready(self) -> bool:
        return bool(self.token)

    def _get(self, path: str, **params: str) -> dict:
        k = path + "?" + "&".join(f"{n}={v}" for n, v in sorted(params.items()))
        with self.lock:
            row = self.db.execute("SELECT fetched, body FROM tmdb WHERE k = ?", (k,)).fetchone()
        if row and row[0] > time.time() - KEPT_FOR:
            return json.loads(row[1])
        try:
            r = self.http.get(path, params=params | self.key_param)
        except httpx.HTTPError as e:
            raise StreamingError(f"TMDB not reachable ({type(e).__name__})") from e  # its text can hold the URL
        if r.status_code == 401:
            raise StreamingError("TMDB refused the token")
        if r.status_code != 200:
            raise StreamingError(f"TMDB answered {r.status_code}")
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO tmdb VALUES (?, ?, ?)", (k, time.time(), r.text))
        return r.json()

    def title_link(self, app: App, tmdb: str, title: str) -> str:
        """The app's own link for the title ("tv/96677" in TMDB's terms), or "" when JustWatch has none."""
        kind, _, tid = tmdb.partition("/")
        if kind not in ("tv", "movie") or not tid.isdigit() or not title.strip():
            return ""
        k = f"jw:{self.region}:{title.strip().lower()}"
        with self.lock:
            row = self.db.execute("SELECT fetched, body FROM tmdb WHERE k = ?", (k,)).fetchone()
        if row and row[0] > time.time() - KEPT_FOR:
            found = json.loads(row[1])
        else:
            try:
                r = self.jw.post(
                    JUSTWATCH, json={"query": JUSTWATCH_QUERY, "variables": {"q": title, "country": self.region}}
                )
                found = r.json() if r.status_code == 200 else {}
            except (httpx.HTTPError, ValueError):
                return ""
            with self.lock, self.db:
                self.db.execute("INSERT OR REPLACE INTO tmdb VALUES (?, ?, ?)", (k, time.time(), json.dumps(found)))
        edges = ((found.get("data") or {}).get("popularTitles") or {}).get("edges") or []
        want = "SHOW" if kind == "tv" else "MOVIE"
        for node in (e.get("node") or {} for e in edges):
            ids = (node.get("content") or {}).get("externalIds") or {}
            if node.get("objectType") != want or str(ids.get("tmdbId")) != tid:
                continue
            for o in node.get("offers") or []:
                name = ((o.get("package") or {}).get("clearName") or "").strip().lower()
                url = o.get("standardWebURL") or ""
                if (
                    o.get("monetizationType") in ("FLATRATE", "FREE", "ADS")
                    and re.search(app.providers, name)
                    and own_link(app, url)
                ):
                    return url
        return ""

    def where(self, kind: str, tmdb_id: int) -> list[App]:
        found = self._get(f"/{kind}/{tmdb_id}/watch/providers")
        return apps_for(found.get("results", {}).get(self.region, {}), self.apps)

    def search(self, q: str) -> list[dict]:
        """Movies and shows matching q that stream on the house's apps: title, year, kind, poster, overview, apps."""
        q = q.strip()
        if not self.ready or len(q) < 2:
            return []
        found = self._get("/search/multi", query=q, include_adult="false", language="en-US").get("results", [])
        return self._on_apps(found, LOOKUPS)

    def suggest(self, q: str, limit: int = 6) -> list[str]:
        """Movie and show titles for search suggestions as you type: one TMDB search (kept a day), no app lookups."""
        q = q.strip()
        if not self.ready or len(q) < 2:
            return []
        found = self._get("/search/multi", query=q, include_adult="false", language="en-US").get("results", [])
        titles = [h.get("title") or h.get("name") for h in found if h.get("media_type") in ("movie", "tv")]
        return list(dict.fromkeys(t for t in titles if t))[:limit]

    def provider_ids(self) -> dict[str, list[int]]:
        """TMDB's provider ids for each of the house's apps in this country (its own list, kept a day)."""
        names: dict[int, str] = {}
        for kind in ("movie", "tv"):
            found = self._get(f"/watch/providers/{kind}", watch_region=self.region, language="en-US")
            for p in found.get("results", []):
                names[p["provider_id"]] = p["provider_name"].strip().lower()
        return {a.key: sorted(i for i, n in names.items() if re.search(a.providers, n)) for a in self.apps}

    def rows(self, per_row: int = 20, at_least: int = 6, today: date | None = None) -> list[dict]:
        """Home's row for each of the house's apps: what is popular on it now in this country (TMDB's discover, by
        the app's provider ids, subscription or free only): movies and shows that came out in the last two years
        (not long-running shows that happen to be airing), each with some votes. Two requests per app, kept a day; a thin row is left out."""
        if not self.ready:
            return []
        today = today or date.today()
        recent = {
            "movie": {"primary_release_date.gte": (today - timedelta(days=730)).isoformat()},
            "tv": {"first_air_date.gte": (today - timedelta(days=730)).isoformat()},
        }
        ids = self.provider_ids()
        out = []
        for app in self.apps:
            if not ids.get(app.key):
                continue
            hits = []
            for kind in ("movie", "tv"):
                found = self._get(
                    f"/discover/{kind}",
                    with_watch_providers="|".join(map(str, ids[app.key])),
                    watch_region=self.region,
                    with_watch_monetization_types="flatrate|free|ads",
                    sort_by="popularity.desc",
                    include_adult="false",
                    language="en-US",
                    **{"vote_count.gte": "50"},
                    **recent[kind],
                )
                hits += [h | {"media_type": kind} for h in found.get("results", [])]
            hits.sort(key=lambda h: h.get("popularity") or 0, reverse=True)
            items = [self._item(h, [app]) for h in hits if h.get("poster_path")][:per_row]
            if len(items) >= at_least:
                out.append({"app": app.key, "name": app.name, "items": items})
        return out

    def catalog(self, per_app: int) -> list[dict]:
        """What is popular on each of the house's apps, then the week's trending titles on them, each once (for
        onapps.py's library): the rows' titles, up to per_app per app."""
        seen, out = set(), []
        for row in self.rows(per_row=per_app, at_least=1):
            for it in row["items"]:
                if it["tmdb"] not in seen:
                    seen.add(it["tmdb"])
                    out.append(it)
        for it in self.trending():
            if it["tmdb"] not in seen:
                seen.add(it["tmdb"])
                out.append(it)
        return out

    def _item(self, h: dict, apps: list[App]) -> dict:
        title = h.get("title") or h.get("name") or ""
        date = h.get("release_date") or h.get("first_air_date") or ""
        return {
            "title": title,
            "year": date[:4],
            "kind": "movie" if h["media_type"] == "movie" else "show",
            "tmdb": f"{h['media_type']}/{h['id']}",
            "thumb": POSTER + h["poster_path"] if h.get("poster_path") else "",
            "backdrop": BACKDROP + h["backdrop_path"] if h.get("backdrop_path") else "",
            "desc": h.get("overview") or "",
            "apps": [link(a, title) for a in apps],
        }

    def trending(self) -> list[dict]:
        """This week's most watched movies and shows (TMDB) that stream on the house's apps: Home's row."""
        if not self.ready:
            return []
        return self._on_apps(self._get("/trending/all/week", language="en-US").get("results", []), TRENDING)

    def _on_apps(self, hits: list[dict], lookups: int) -> list[dict]:
        hits = [h for h in hits if h.get("media_type") in ("movie", "tv")][:lookups]

        def one(h):
            try:
                apps = self.where(h["media_type"], h["id"])
            except StreamingError:
                return None
            if not apps:
                return None
            return self._item(h, apps)

        with ThreadPoolExecutor(LOOKUPS) as pool:
            return [r for r in pool.map(one, hits) if r]


def tv_quiet(open_app, *args) -> None:
    """open_app from a background thread: a TV that does not answer is reported, never raised."""
    try:
        open_app(*args)
    except TvError as e:
        print("tv:", e)


class Open(BaseModel):
    app: str
    title: str = ""
    tmdb: str = ""  # "tv/96677": then the app opens at the title itself where JustWatch has its link


LAUNCH_CLIP = Path(__file__).parent / "static" / "launch.mp4"  # 3 s of black, what the player shows meanwhile


def router(
    streaming: Streaming,
    open_app: Callable[[str, str, str], None] | None,
    from_tv: Callable[[Request], bool] = lambda request: False,
) -> APIRouter:
    api = APIRouter(prefix="/api/streaming")

    @api.get("/status")
    def status():
        return {"ready": streaming.ready, "tv": open_app is not None, "apps": [a.name for a in streaming.apps]}

    @api.get("/search")
    def search(q: str):
        try:
            return streaming.search(q)
        except StreamingError as e:
            raise HTTPException(502, str(e)) from e

    @api.get("/suggest")
    def suggest(q: str):
        try:
            return streaming.suggest(q)
        except StreamingError:
            return []  # suggestions are a nicety: no error while typing

    @api.get("/rows")
    def rows():
        try:
            return streaming.rows()
        except StreamingError as e:
            raise HTTPException(502, str(e)) from e

    @api.get("/trending")
    def trending():
        try:
            return streaming.trending()
        except StreamingError as e:
            raise HTTPException(502, str(e)) from e

    @api.post("/open", status_code=204)
    def open_on_tv(body: Open):
        """📺 on a streaming app: opens it on the TV (at its search for the title where it can)."""
        app = next((a for a in streaming.apps if a.key == body.app), None)
        if app is None:
            raise HTTPException(404, "not one of the house's apps")
        if open_app is None:
            raise HTTPException(409, "No TV set up (add one on the Settings page)")
        try:
            open_app(app.packages, body.title[:200], streaming.title_link(app, body.tmdb, body.title[:200]))
        except TvError as e:
            raise HTTPException(502, str(e)) from e

    @api.get("/launch")
    def launch(request: Request, app: str, tmdb: str = "", title: str = ""):
        """An On my apps title played in a Jellyfin app (onapps.py's .strm): on the TV, opens the streaming app at the
        title. Anyone else (Jellyfin reading the file, a phone) only gets the clip, so nothing opens by surprise."""
        found = next((a for a in streaming.apps if a.key == app), None)
        if found is None:
            raise HTTPException(404, "not one of the house's apps")
        if open_app is not None and from_tv(request):
            name = title[:200]
            link = streaming.title_link(found, tmdb, name)
            threading.Thread(target=lambda: tv_quiet(open_app, found.packages, name, link), daemon=True).start()
        return FileResponse(LAUNCH_CLIP, media_type="video/mp4")

    @api.get("/go")
    def go(app: str, title: str = "", tmdb: str = ""):
        """The phone's app button: to the title's own link in the app (which opens the app), else its search."""
        found = next((a for a in streaming.apps if a.key == app), None)
        if found is None:
            raise HTTPException(404, "not one of the house's apps")
        url = streaming.title_link(found, tmdb, title[:200]) or link(found, title[:200])["web"]
        return RedirectResponse(url, status_code=302)

    return api
