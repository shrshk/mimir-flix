"""Provider plugins (catalog.py) through a fake plugin: a small catalogue with a show (two seasons), movies, and a
live channel whose stream is plain http and whose link expires."""

import base64
import hashlib
import json
import os
import time
from urllib.parse import unquote, parse_qs, quote, urlsplit

import httpx
from fastapi import Request
import pytest
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from jobkit import Jobs

from mimir_flix import checks, mirror, settings
from mimir_flix import jobs as app_jobs
from mimir_flix.app import create_app
from mimir_flix.catalog import Catalog, CatalogError, Catalogs, Job, node_id
from mimir_flix.catalog_routes import hidden_libraries, listing, plugin_paths
from mimir_flix.catalog_routes import router
from mimir_flix import hls as hls_mod
from mimir_flix.hls import HlsError, HlsRelay, from_tailnet
from mimir_flix.jellyfin import Jellyfin, JellyfinError
from mimir_flix.kodi import Kodi
from mimir_flix.mcp_server import router as mcp_router
from mimir_flix.providers import Providers
from mimir_flix.providers import router as providers_router
from mimir_flix.resolver import start_resolver
from mimir_flix.strm import follow, followed, refresh_shows, show_folder, write_movie, write_show
from mimir_flix.vault import Vault
from mimir_flix.watchlist import Watchlist
from mimir_flix.watchlist import router as list_router
from tests.test_library import FakeJellyfin

SITE = "https://cat.example"
SECTIONS = {"Movies": f"{SITE}/movies", "Shows": f"{SITE}/shows", "Live": f"{SITE}/live"}
SHOW = f"{SITE}/shows/dark"
S1, S2 = f"{SHOW}/s1", f"{SHOW}/s2"
EP = "https://files.example/dark/s{s}e{e}.mp4"
MOVIE = "https://files.example/movies/heat.mkv"
STREAM = "http://203.0.113.9:8080/1/index.m3u8"


def channel(exp: float) -> str:
    return f"{SITE}/ch/1?exp={int(exp)}"


def video(title, url, here=False):
    return {"title": title, "url": url, "thumb": "", "desc": "", "kind": "video", "here": here}


def folder(title, url, thumb=""):
    return {"title": title, "url": url, "thumb": thumb, "desc": f"About {title}", "kind": "folder", "here": False}


class FakeCatalog(Catalog):
    """The provider's side: pages, signing (a signed link is the file's address with ?sig=), a channel that only
    plays while its link has not expired (its list gives a fresh one), and a login check."""

    def __init__(self, login, data):
        self.login, self.data = login, data
        self.pages = {
            SECTIONS["Movies"]: [video("Heat (1995)", MOVIE), video("Up (2009)", "https://files.example/up.mp4", True)],
            SECTIONS["Shows"]: [folder("Dark", SHOW, "http://thumbs.example/dark.jpg"), folder("Other", f"{SITE}/o")],
            SHOW: [folder("Season 2", S2), folder("Season 1", S1)],
            S1: [video("Episode 2. Lies", EP.format(s=1, e=2)), video("Episode 1", EP.format(s=1, e=1))],
            S2: [video("Episode 1. Truth", EP.format(s=2, e=1))],
            SECTIONS["Live"]: [video("Sports 1", channel(time.time() + 3600), True)],
        }
        self.fetched: list[str] = []
        self.signed = 0
        self.index_runs = 0

    def sections(self):
        return [{"name": n, "url": u} for n, u in SECTIONS.items()]

    def home(self, lang):
        return [{"section": "Movies", "url": SECTIONS["Movies"], "items": self.page(SECTIONS["Movies"])}]

    def page(self, url, fresh=False, ttl=None):
        if url not in self.pages:
            raise CatalogError("no such page")
        self.fetched.append(url)
        return [dict(i) for i in self.pages[url]]

    def owns(self, url):
        return url.startswith(SITE + "/")

    def plays(self, url):  # its videos are on a file host of its own too
        return self.owns(url) or url.startswith("https://files.example/")

    def link(self, url, page=""):
        if url.startswith(f"{SITE}/ch/"):
            if int(parse_qs(urlsplit(url).query)["exp"][0]) < time.time():
                if not page:
                    raise CatalogError(f"this channel's link expired: {url}")
                return self.link(self.page(page)[0]["url"])
            return STREAM
        self.signed += 1
        return f"{url}?sig=1"

    def check(self):
        if self.login()["password"] == "nope":
            raise CatalogError("the provider refused the login")

    def search(self, q, lang=""):
        hits = [i for p in self.pages.values() for i in p if q.lower() in i["title"].lower()]
        return [{"section": "Everything", "items": hits}] if hits else []

    def suggest(self, q):
        return ["Dark", "Dark"] if q.lower().startswith("da") else []

    def live(self, q):
        return [
            {"title": "Sports 1", "section": "Live", "url": self.pages[SECTIONS["Live"]][0]["url"]}
            | {"page": SECTIONS["Live"], "quality": "720p", "why": "matches 1 of 1 words"}
        ]

    def trail(self, url):
        return [p for p in urlsplit(url).path.split("/") if p]

    def relayed(self, url):
        return url.startswith(f"{SITE}/ch/")

    def on_demand(self, url):
        return not self.relayed(url)

    def kodi(self, url, title):
        return f"plugin://plugin.video.fake/?play={quote(url)}" if url.endswith(".mp4") else ""

    def thumb_ok(self, url):
        return urlsplit(url).netloc == "posters.example"

    def section_of(self, url):
        return "Shows"

    def status(self):
        return "rebuilding the index"

    def jobs(self):
        return [Job("index", "the show index", "weekly", self.index, due=lambda: self.index_runs == 0)]

    def info(self, base):
        return [{"label": "Playlist", "value": f"{base}/fake.m3u"}]

    def serve(self, path, query, ctx):  # a playlist for other players, its links through core's resolver
        if path != "fake.m3u":
            return None
        return PlainTextResponse(f"#EXTM3U\n#EXTINF:-1,Heat\n{ctx.play(MOVIE, '')}\n# from {ctx.base}\n")

    def index(self):
        self.index_runs += 1
        return "index rebuilt"

    def everything(self):
        shows = [i for i in self.pages[SECTIONS["Shows"]] if i["kind"] == "folder"]
        movies = [{"kind": "movie", "url": MOVIE, "title": "Heat (1995)", "page": SECTIONS["Movies"]}]
        return movies + getattr(self, "extra_movies", []) + [
            {"kind": "show", "url": s["url"], "title": s["title"], "page": SECTIONS["Shows"]} for s in shows
        ]


class Plugin:
    kind = "fakecat"
    label = "Fake catalogue"
    fields = [
        {"key": "user", "label": "Username"},
        {"key": "password", "label": "Password"},
        {"key": "player", "label": "Player add-on, optional", "optional": True},
    ]

    def __init__(self):
        self.made = []

    def make(self, login, data):
        self.made.append(FakeCatalog(login, data))
        return self.made[-1]


@pytest.fixture
def plugin():
    return Plugin()


@pytest.fixture
def home(tmp_path, plugin):
    """A Mac with one plugin provider, "My TV" (id tv1), its login in the vault."""
    config, data = tmp_path / "config", tmp_path / "data"
    vault = Vault(tmp_path / "m.db", lambda: b"k" * 32)
    vault.put("provider:tv1", {"user": "me", "password": "pw"})
    settings.save({"providers": [{"id": "tv1", "name": "My TV", "kind": "fakecat"}]}, config)
    return Catalogs(config, vault, data, kinds={"fakecat": plugin})


@pytest.fixture
def cat(home):
    return home.get("tv1")[1]


def app(home, tmp_path, jellyfin, **kw):
    routes = router(
        home, jellyfin, kw.pop("kodi", None), tmp_path / "media", "http://air:8724", tmp_path / "thumbs", **kw
    )
    return TestClient(create_app(jellyfin, tmp_path / "picks", routes))


def test_routes_browse_play_and_add_to_jellyfin(home, cat, tmp_path, monkeypatch):
    jf = FakeJellyfin()
    played = []

    def kodi_rpc(request: httpx.Request) -> httpx.Response:
        played.append(json.loads(request.read())["params"]["item"]["file"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "OK"})

    kodi = Kodi("http://tv:8080", "pw", transport=httpx.MockTransport(kodi_rpc))
    jellyfin = Jellyfin("http://jf", "key", transport=httpx.MockTransport(jf.handler))
    c = app(home, tmp_path, jellyfin, kodi=kodi)

    root = c.get("/api/p/tv1/browse").json()
    assert root["title"] == "My TV" and [i["title"] for i in root["items"]] == ["Movies", "Shows", "Live"]
    shows = c.get("/api/p/tv1/browse", params={"node": root["items"][1]["node"]}).json()
    dark = shows["items"][0]
    assert (dark["title"], dark["p"], dark["node"]) == ("Dark", "tv1", node_id("tv1", SHOW))
    assert c.get("/api/p/tv1/browse", params={"node": node_id("tv1", "https://evil.example/")}).status_code == 400
    assert c.get("/api/p/tv1/browse", params={"node": node_id("tv2", SHOW)}).status_code == 400  # another's node
    assert c.get("/api/p/nope/browse").status_code == 404
    rows = c.get("/api/p/tv1/home").json()
    assert [(r["section"], [i["title"] for i in r["items"]]) for r in rows] == [
        ("Movies", ["Heat (1995)", "Up (2009)"])
    ]

    # Jellyfin first: a movie lands in "My TV Movies", a live channel in "My TV Live"; the id comes back.
    movie = c.post("/api/p/tv1/jellyfin/video", json={"url": MOVIE, "title": "Heat (1995)"})
    assert movie.json() == {"id": "strm:Heat (1995)"} and jf.played == []
    line = (tmp_path / "media/tv1-movies/Heat (1995)/Heat (1995).strm").read_text().strip()
    assert parse_qs(urlsplit(line).query) == {"p": ["tv1"], "path": [MOVIE]}
    live_url = cat.pages[SECTIONS["Live"]][0]["url"]
    live = c.post(
        "/api/p/tv1/jellyfin/video",
        json={"url": live_url, "title": "Sports 1", "tv": True, "page": node_id("tv1", SECTIONS["Live"])},
    )
    assert live.json() == {"id": "strm:Sports 1"} and jf.played == ["strm:Sports 1"]  # started in the TV app
    assert jf.libs["My TV Live"]["Locations"] == [str(tmp_path / "media/tv1-live")]
    assert c.post("/api/p/tv1/jellyfin/video", json={"url": "file:///etc/passwd"}).status_code == 400

    # A show: the whole show, or one episode of it (found by its file).
    whole = c.post("/api/p/tv1/jellyfin", json={"node": dark["node"], "title": "Dark"}).json()
    assert whole == {"id": "series:Dark", "episodes": 3} and "My TV" in jf.libs
    one = c.post(
        "/api/p/tv1/jellyfin", json={"node": dark["node"], "title": "Dark", "episode": EP.format(s=1, e=2), "tv": True}
    ).json()
    assert one["id"] == "strm:Dark S01E02 - Lies" and jf.played[-1] == one["id"]
    bad = {"node": dark["node"], "title": "Dark", "episode": "https://x"}
    assert c.post("/api/p/tv1/jellyfin", json=bad).status_code == 404

    # What Jellyfin has says so (asked through its API): the movie, the show, its episodes.
    rows = c.get("/api/p/tv1/home").json()
    assert {i["title"]: i["jf_id"] for i in rows[0]["items"]} == {"Heat (1995)": "strm:Heat (1995)", "Up (2009)": ""}
    assert c.get("/api/p/tv1/browse", params={"node": shows["items"][0]["node"]}).json()["jf_id"] == "series:Dark"
    assert all(i["jf_id"] for i in c.get("/api/p/tv1/browse", params={"node": node_id("tv1", S1)}).json()["items"])
    # Removed in Jellyfin (the file is still on disk): no longer marked.
    jf.removed.add("strm:Heat (1995)")
    monkeypatch.setattr("mimir_flix.catalog_routes.time.monotonic", lambda: 10**9)  # past the few seconds of reuse
    assert c.get("/api/p/tv1/home").json()[0]["items"][0]["jf_id"] == ""
    # The provider's libraries stay out of Your library.
    assert hidden_libraries(home) == {"My TV", "My TV Movies", "My TV Live"}

    # Playing here: a 302 to the signed link; Kodi: the provider's own add-on, or the stream itself.
    r = c.get("/api/p/tv1/play", params={"u": EP.format(s=1, e=1)}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == EP.format(s=1, e=1) + "?sig=1"
    assert c.post("/api/p/tv1/kodi", json={"url": EP.format(s=1, e=1), "title": "Dark 1"}).json() == {
        "playing": "Dark 1"
    }
    assert c.post("/api/p/tv1/kodi", json={"url": MOVIE, "title": "Heat"}).status_code == 200
    assert played == [f"plugin://plugin.video.fake/?play={quote(EP.format(s=1, e=1))}", MOVIE + "?sig=1"]

    # Search, suggestions and the provider's status note.
    found = c.get("/api/p/tv1/search", params={"q": "heat"}).json()
    assert [(g["section"], [i["title"] for i in g["items"]]) for g in found] == [("Everything", ["Heat (1995)"])]
    assert c.get("/api/p/tv1/suggest", params={"q": "da"}).json() == ["Dark"]
    assert c.get("/api/p/tv1/status").json() == {"note": "rebuilding the index"}


def test_thumbnails_only_from_what_a_catalogue_listed_or_vouches_for(home, tmp_path):
    c = app(home, tmp_path, Jellyfin("http://jf", "k"))
    enc = lambda u: base64.urlsafe_b64encode(u.encode()).decode()  # noqa: E731
    assert c.get("/api/thumb", params={"u": enc("http://192.0.2.1/admin")}).status_code == 404
    # A poster cached before a restart still shows.
    seen = "http://img.example/old.jpg"
    (tmp_path / "thumbs" / (hashlib.sha256(seen.encode()).hexdigest()[:24] + ".jpg")).write_bytes(b"jpg")
    assert c.get("/api/thumb", params={"u": enc(seen)}).content == b"jpg"


def test_the_resolver_signs_relays_and_reads_old_links_as_the_first_providers(home, cat, tmp_path, caplog):
    def stream_server(request: httpx.Request) -> httpx.Response:
        hls = {"Content-Type": "application/vnd.apple.mpegurl"}
        if request.url.path == "/1/index.m3u8":
            return httpx.Response(200, text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv/mono.m3u8\n", headers=hls)
        if request.url.path == "/1/v/mono.m3u8":
            return httpx.Response(200, text="#EXTM3U\n#EXTINF:6,\nseg1.ts\n", headers=hls)
        if request.url.path == "/1/v/seg1.ts":
            return httpx.Response(200, content=b"TS-BYTES", headers={"Content-Type": "video/MP2T"})
        return httpx.Response(404)

    server = start_resolver(home, 0, "127.0.0.1", hls=HlsRelay(transport=httpx.MockTransport(stream_server)))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # On demand: a 302 to a freshly signed link. A line from before providers had ids (no p=) still plays.
        movie = write_movie("tv1", MOVIE, "Heat", tmp_path, base).read_text().strip()
        r = httpx.get(movie)
        assert r.status_code == 302 and r.headers["location"] == MOVIE + "?sig=1"
        r = httpx.get(f"{base}/play?path={quote(MOVIE, safe='')}")
        assert r.status_code == 302 and r.headers["location"] == MOVIE + "?sig=1"
        assert httpx.get(f"{base}/play?p=gone&path={quote(MOVIE, safe='')}").status_code == 404
        assert httpx.get(f"{base}/play", params={"path": "file:///etc/passwd"}).status_code == 404

        # A live channel saved hours ago: its list gives the fresh link; the plain-http stream is relayed, every
        # playlist line coming back through /hls.
        old = channel(time.time() - 60)
        line = write_movie("tv1", old, "Sports 1", tmp_path, base, SECTIONS["Live"]).read_text().strip()
        r = httpx.get(line)
        assert r.status_code == 302 and r.headers["location"] == "/hls?u=" + quote(STREAM, safe="") + "&x=.m3u8"
        master = httpx.get(base + r.headers["location"]).text
        variant = httpx.get(base + master.splitlines()[-1]).text
        assert variant.splitlines()[-1].endswith("&x=.ts")
        assert httpx.get(base + variant.splitlines()[-1]).content == b"TS-BYTES"
        # Only stream servers a provider handed out are relayed.
        assert httpx.get(base + "/hls?u=" + quote("http://192.0.2.1/admin", safe="")).status_code == 404
        # A play that fails says why in the log, never the link.
        r = httpx.get(f"{base}/play?p=tv1&path={quote(old, safe='')}")
        assert r.status_code == 502 and "expired" in caplog.text and "exp=" not in caplog.text
    finally:
        server.shutdown()


def test_away_from_home_this_mac_streams_the_file(home, tmp_path):
    data = bytes(range(256)) * 64

    def files(request: httpx.Request) -> httpx.Response:
        a, b = (int(x) for x in request.headers["range"].split("=")[1].split("-"))
        headers = {"Content-Type": "video/mp4", "Content-Range": f"bytes {a}-{b}/{len(data)}"}
        return httpx.Response(206, content=data[a : b + 1], headers=headers)

    away = {"X-Forwarded-For": "100.64.0.10", "Range": "bytes=1000-1999"}  # a Tailscale address, via Caddy
    server = start_resolver(home, 0, "127.0.0.1", hls=HlsRelay(transport=httpx.MockTransport(files)))
    try:
        r = httpx.get(
            f"http://127.0.0.1:{server.server_address[1]}/play", params={"p": "tv1", "path": MOVIE}, headers=away
        )
        assert r.status_code == 206 and r.content == data[1000:2000]
    finally:
        server.shutdown()
    c = app(home, tmp_path, Jellyfin("http://jf", "k"), hls=HlsRelay(transport=httpx.MockTransport(files)))
    c = TestClient(c.app, client=("127.0.0.1", 5000))
    r = c.get("/api/p/tv1/play", params={"u": MOVIE}, headers=away)
    assert r.status_code == 206 and r.content == data[1000:2000]


def test_only_caddy_on_this_machine_may_say_where_a_player_is():
    assert from_tailnet("100.64.0.10")  # straight from the tailnet
    assert from_tailnet("127.0.0.1", "100.64.0.10, 127.0.0.1")  # through Caddy
    assert not from_tailnet("127.0.0.1", "192.0.2.50")  # through Caddy, at home
    assert not from_tailnet("192.0.2.50", "100.64.0.10")  # a home device cannot claim to be away
    assert not from_tailnet("127.0.0.1", "nonsense")


def test_fmp4_segments_are_relayed_as_mp4():
    # ffmpeg (Jellyfin) refuses a segment address ending in .fmp4; the bytes are mp4.
    out = HlsRelay().rewrite('#EXT-X-MAP:URI="init.hls.fmp4"\nseg-1.hls.fmp4\n', "http://1.2.3.4:80/a/i.m3u8", "/h?u=")
    assert [line.rsplit("&x=", 1)[-1].rstrip('"') for line in out.splitlines()] == [".mp4", ".mp4"]


def test_a_show_becomes_jellyfin_episodes_and_new_ones_follow(cat, tmp_path):
    root = tmp_path / "strm"
    files = write_show(cat, "tv1", SHOW, "Dark", root, "http://air:8724")
    assert len(files) == 3 and files[EP.format(s=1, e=1)].name == "Dark S01E01.strm"
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*.strm")) == [
        "Dark/Season 01/Dark S01E01.strm",
        "Dark/Season 01/Dark S01E02 - Lies.strm",
        "Dark/Season 02/Dark S02E01 - Truth.strm",
    ]
    follow(root, "Dark", SHOW)
    cat.pages[S2] = [video("Episode 2. New", EP.format(s=2, e=2)), *cat.pages[S2]]  # a new episode drops
    assert refresh_shows(cat, "tv1", root, "http://air:8724") == 1
    assert (root / "Dark/Season 02/Dark S02E02 - New.strm").exists()
    assert refresh_shows(cat, "tv1", root, "http://air:8724") == 0  # nothing new the second time
    # The same name from another section gets its own folder, labelled with where it came from.
    assert show_folder(root, "Dark", f"{SITE}/other/dark", cat) == "Dark (Shows)"
    assert show_folder(root, "DARK", SHOW, cat) == "Dark"  # adding again keeps its folder


def test_files_already_there_are_left_alone(tmp_path):
    # Jellyfin wipes what it knows about a changed file, and a TV play landing then finds nothing to play: adding
    # again, or a file written before providers had ids, stays as it is.
    target = write_movie("tv1", MOVIE, "Heat (1995)", tmp_path, "http://air")
    os.utime(target, (1, 1))
    write_movie("tv1", MOVIE, "Heat (1995)", tmp_path, "http://air")
    assert target.stat().st_mtime == 1
    target.write_text(f"http://air/play?path={quote(MOVIE, safe='')}\n")
    os.utime(target, (1, 1))
    write_movie("tv1", MOVIE, "Heat (1995)", tmp_path, "http://air")
    assert target.stat().st_mtime == 1 and "p=" not in target.read_text()


def test_my_list_saves_pointers_and_marks_new_episodes(home, cat, tmp_path):
    jf = FakeJellyfin()
    jellyfin = Jellyfin("http://jf", "key", transport=httpx.MockTransport(jf.handler))
    wl = Watchlist(tmp_path / "mimir-flix.db")
    c = app(home, tmp_path, jellyfin, watchlist=wl)
    dark = c.get("/api/p/tv1/browse", params={"node": node_id("tv1", SECTIONS["Shows"])}).json()["items"][0]

    # A show and a video: only a pointer is kept; nothing goes into Jellyfin or onto the drive.
    body = {"node": dark["node"], "title": "Dark", "thumb": dark["thumb"]}
    assert c.post("/api/p/tv1/list", json=body).json() == {"key": dark["node"]}
    c.post("/api/p/tv1/list", json={"url": MOVIE, "title": "Heat (1995)", "thumb": "/api/thumb?u=bad"})
    assert c.post("/api/p/tv1/list", json={"url": "file:///etc/passwd", "title": "x"}).status_code == 400
    assert jf.libs == {} and not (tmp_path / "media").exists()
    wl.check(home, only=SHOW)  # what the show has when saved (the route does this in the background)
    saved = c.get("/api/p/tv1/list").json()
    assert [(i["title"], i["kind"], i["key"], i["new"], i["p"]) for i in saved] == [
        ("Heat (1995)", "video", MOVIE, 0, "tv1"),
        ("Dark", "folder", dark["node"], 0, "tv1"),
    ]
    assert saved[0]["thumb"] == "" and saved[1]["thumb"] == dark["thumb"]  # only a thumbnail it listed is kept

    # The hourly check: a new episode drops; the list says so until the show is opened.
    cat.pages[S2] = [video("Episode 2. New", EP.format(s=2, e=2)), *cat.pages[S2]]
    assert wl.check(home, ttl=0) == 1 and wl.check(home, ttl=0) == 0
    assert {i["title"]: i["new"] for i in c.get("/api/p/tv1/list").json()}["Dark"] == 1
    c.get("/api/p/tv1/browse", params={"node": dark["node"]})
    assert {i["title"]: i["new"] for i in c.get("/api/p/tv1/list").json()}["Dark"] == 0

    # An entry saved before providers had ids belongs to the first provider.
    wl.add(f"{SITE}/shows/old", "show", "Old show")
    assert "Old show" in [i["title"] for i in c.get("/api/p/tv1/list").json()]

    # Saving again keeps it once; removing is one call by the same key.
    c.post("/api/p/tv1/list", json={"url": MOVIE, "title": "Heat (1995)"})
    assert len(c.get("/api/p/tv1/list").json()) == 3
    assert c.delete("/api/p/tv1/list", params={"key": MOVIE}).status_code == 204
    assert c.delete("/api/p/tv1/list", params={"key": dark["node"]}).status_code == 204
    assert c.delete("/api/p/tv1/list", params={"key": MOVIE}).status_code == 404

    # A title Jellyfin has is a Jellyfin favourite, like anything in the library: no copy stays on this list.
    c.post("/api/p/tv1/list", json={"url": MOVIE, "title": "Heat (1995)"})
    c.post("/api/p/tv1/jellyfin/video", json={"url": MOVIE, "title": "Heat (1995)"})  # added after it was listed
    assert "Heat (1995)" not in [i["title"] for i in c.get("/api/p/tv1/list").json()]
    assert jf.favourites == {"strm:Heat (1995)"} and MOVIE not in [i["url"] for i in wl.items()]
    c.post("/api/p/tv1/jellyfin", json={"node": dark["node"], "title": "Dark"})
    assert c.post("/api/p/tv1/list", json=body).json() == {"key": dark["node"], "jellyfin_id": "series:Dark"}
    assert "series:Dark" in jf.favourites and [i["title"] for i in wl.items()] == ["Old show"]


def test_hourly_jobs_record_each_run_under_the_providers_id(home, cat, tmp_path):
    root = tmp_path / "media" / "tv1"
    write_show(cat, "tv1", SHOW, "Dark", root, "http://air:8724")
    follow(root, "Dark", SHOW)
    jobs = Jobs(tmp_path / "mimir-flix.db", "mimir-flix")
    app_jobs.define(jobs)
    fake = FakeJellyfin()
    jf = Jellyfin("http://jf", "k", transport=httpx.MockTransport(fake.handler))
    cat.pages[S2] = [video("Episode 2. New", EP.format(s=2, e=2)), *cat.pages[S2]]
    assert app_jobs.new_episodes(jobs, home, jf, tmp_path / "media", "http://air:8724") == 1
    down = Jellyfin("http://jf", "k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    cat.pages[S2] = [video("Episode 3. Newer", EP.format(s=2, e=3)), *cat.pages[S2]]
    with pytest.raises(JellyfinError):
        app_jobs.new_episodes(jobs, home, down, tmp_path / "media", "http://air:8724")

    # The provider's own job: named after its id, run when due; Run now runs it either way.
    [(name, job)] = app_jobs.provider_jobs(jobs, home)
    assert name == "tv1-index" and job.due()
    app_jobs.run_job(jobs, name, job)
    assert not job.due() and cat.index_runs == 1
    c = TestClient(create_app(jf, tmp_path / "picks", app_jobs.router(jobs)), client=("127.0.0.1", 5000))
    body = {j["name"]: j for j in c.get("/api/jobs").json()["jobs"]}
    assert body["tv1-index"]["description"] == "My TV: the show index" and body["tv1-index"]["last"]["status"] == "ok"
    assert body["new-episodes"]["last"]["status"] == "failed" and "500" in body["new-episodes"]["last"]["error"]
    wifi = TestClient(c.app, client=("192.0.2.30", 5000))
    assert wifi.post("/api/jobs/tv1-index/run").status_code == 403  # jobs run and pause from the Mac or the tailnet
    assert wifi.post("/api/jobs/backup/pause", json={"paused": True}).status_code == 403
    assert c.post("/api/jobs/tv1-index/run").status_code == 202
    for _ in range(50):
        if cat.index_runs == 2:
            break
        time.sleep(0.02)
    assert cat.index_runs == 2


def test_settings_add_a_plugin_provider_after_checking_its_login(tmp_path, plugin):
    config, data = tmp_path / "config", tmp_path / "data"
    vault = Vault(tmp_path / "m.db", lambda: b"k" * 32)
    catalogs = Catalogs(config, vault, data, kinds={"fakecat": plugin})
    providers = Providers(config, vault, lambda: None, 8723, catalogs=catalogs, base="https://lan.example")
    routes = providers_router(providers, trusted)
    paths = plugin_paths(catalogs, "http://air:8724")
    api = TestClient(create_app(Jellyfin("http://jf", "k"), tmp_path, routes, other_paths=paths))

    assert api.get("/api/settings/providers/kinds").json() == [
        {"kind": "fakecat", "label": "Fake catalogue", "fields": Plugin.fields}
    ]
    wrong = {"name": "Mine", "kind": "fakecat", "user": "me", "password": "nope"}
    r = api.post("/api/settings/providers", json=wrong)
    assert r.status_code == 502 and "refused" in r.json()["detail"] and catalogs.all() == []
    assert vault.db.execute("SELECT count(*) FROM vault").fetchone()[0] == 0  # a login that failed is not kept
    assert api.post("/api/settings/providers", json=wrong | {"password": ""}).status_code == 422
    assert api.post("/api/settings/providers", json=wrong | {"kind": "nothing"}).status_code == 422
    assert api.post("/api/settings/providers", json=wrong | {"player": ["x"]}).status_code == 422

    pid = api.post("/api/settings/providers", json=wrong | {"password": "s3cret-login"}).json()["id"]
    assert api.get("/api/settings/providers").json() == [
        {"id": pid, "name": "Mine", "kind": "fakecat", "guide": False, "label": "Fake catalogue"}
        | {"info": [{"label": "Playlist", "value": "https://lan.example/fake.m3u"}]}  # the plugin's own lines
    ]
    # A path of the plugin's own: its links go through the resolver; core's routes and 404s are unchanged.
    feed = api.get("/fake.m3u").text
    assert f"http://air:8724/play?p={pid}&path=https%3A%2F%2Ffiles.example%2Fmovies%2Fheat.mkv" in feed
    assert "# from http://testserver" in feed
    assert api.get("/nothing.php").status_code == 404 and api.get("/api/settings/providers/kinds").status_code == 200
    assert listing(catalogs) == [{"id": pid, "name": "Mine"}]  # picked up at once, no restart
    stored = (config / "settings.json").read_text() + (tmp_path / "m.db").read_bytes().decode("latin-1")
    assert "s3cret-login" not in stored and vault.get(f"provider:{pid}")["password"] == "s3cret-login"
    assert vault.get(f"provider:{pid}")["player"] == ""  # an optional field may stay empty
    with_player = wrong | {"name": "Other", "password": "s3cret-login", "player": "plugin.video.x", "epg": "kept out"}
    other = api.post("/api/settings/providers", json=with_player).json()["id"]
    assert vault.get(f"provider:{other}") == {"user": "me", "password": "s3cret-login", "player": "plugin.video.x"}
    api.delete(f"/api/settings/providers/{other}")
    found = {r["name"]: r for r in checks.run(config, vault=vault, catalogs=catalogs)}
    assert found["Mine"]["ok"] is True

    assert api.delete(f"/api/settings/providers/{pid}").status_code == 204
    assert catalogs.all() == [] and vault.get(f"provider:{pid}") is None


def test_without_a_plugin_installed_its_provider_is_left_out(tmp_path, home):
    bare = Catalogs(home.config, home.vault, home.data, kinds={})
    assert bare.all() == [] and bare.default() is None


def test_the_assistant_searches_plays_and_saves_a_providers_titles(home, cat, tmp_path):
    jf = FakeJellyfin()
    jellyfin = Jellyfin("http://jf", "key", transport=httpx.MockTransport(jf.handler))
    wl = Watchlist(tmp_path / "mimir-flix.db")
    routes = router(home, jellyfin, None, tmp_path / "media", "http://air:8724", tmp_path / "thumbs", watchlist=wl)
    connectors = {"providers": lambda: listing(home)}
    a = create_app(jellyfin, tmp_path / "picks", routes, list_router(wl, dict), mcp_router("t"), connectors=connectors)
    c = TestClient(a, client=("127.0.0.1", 5000))

    def tool(name, **args):
        r = c.post(
            "/mcp",
            headers={"Authorization": "Bearer t"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
        ).json()["result"]
        return (r["content"][0]["text"], True) if r["isError"] else json.loads(r["content"][0]["text"])

    found = tool("search", query="dark")["providers"]
    assert found == [
        {
            "provider": "tv1",
            "name": "My TV",
            "section": "Everything",
            "items": [{"title": "Dark", "sub": "", "id": node_id("tv1", SHOW), "kind": "show"}],
        }
    ]
    show = found[0]["items"][0]
    assert tool("add_to_jellyfin", provider="tv1", id=show["id"], title="Dark") == {
        "jellyfin_id": "series:Dark",
        "episodes": 3,
    }
    live = tool("live_now", query="sports")
    assert [(s["provider"], s["title"]) for s in live] == [("tv1", "Sports 1")]
    played = tool("play_on_tv", provider="tv1", id=live[0]["id"], page=live[0]["page"], title="Sports 1")
    assert played == {"playing": "Sports 1", "jellyfin_id": "strm:Sports 1"} and jf.played == ["strm:Sports 1"]

    # My list: by provider and id; an older argument name (url, or no provider) still lands.
    # Dark is in Jellyfin (added above): a Jellyfin favourite, so it lists with the library, not here.
    added = tool("add_to_my_list", provider="tv1", id=show["id"], title="Dark")
    assert added == {"key": show["id"], "jellyfin_id": "series:Dark"} and jf.favourites == {"series:Dark"}
    assert tool("add_to_my_list", url=MOVIE, title="Heat") == {"key": MOVIE}
    mine = tool("my_list")["providers"]
    assert {(t["title"], t["kind"], t["provider"], t["id"]) for t in mine} == {("Heat", "video", "tv1", MOVIE)}
    assert tool("remove_from_my_list", provider="tv1", id=MOVIE) == {"removed": MOVIE}


def trusted(request: Request) -> None:
    """Every caller may change settings (the access rule has tests of its own, in test_providers)."""


def test_the_relay_never_reaches_this_mac_whatever_a_stream_server_says(tmp_path):
    # A hostile stream server lists, or redirects to, this Mac's own apps (which trust this Mac): never fetched.
    asked = []

    def server(r):
        asked.append(str(r.url))
        if r.url.path == "/moved.m3u8":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8723/iptv/p1/playlist.m3u"})
        return httpx.Response(200, text="#EXTM3U\n")

    relay = HlsRelay(transport=httpx.MockTransport(server))
    out = relay.rewrite("#EXTM3U\nhttp://127.0.0.1:8723/api/settings\nhttp://[::1]:8096/Auth/Keys\n", "http://1.2.3.4/a.m3u8", "/h?u=")
    for line in out.splitlines()[1:]:
        with pytest.raises(HlsError, match="this Mac"):
            relay.open(unquote(line.split("u=", 1)[1].split("&x=")[0]))
    relay.allow("http://1.2.3.4/moved.m3u8")
    with pytest.raises(HlsError, match="this Mac"):
        relay.open("http://1.2.3.4/moved.m3u8")
    assert asked == ["http://1.2.3.4/moved.m3u8"]  # the redirect was never followed
    relay.open("http://1.2.3.4/a.m3u8").close()  # a stream server itself is fine


def test_a_video_the_provider_never_listed_is_never_fetched(home, tmp_path):
    # A caller's own address (a router on the LAN) is refused before the provider is asked or this Mac fetches it.
    fetched = []

    def anything(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, content=b"router admin page", headers={"Content-Type": "text/html"})

    c = app(home, tmp_path, Jellyfin("http://jf", "key"), hls=HlsRelay(transport=httpx.MockTransport(anything)))
    away = TestClient(c.app, client=("127.0.0.1", 5000), headers={"X-Forwarded-For": "100.64.0.10"})
    for u in ("https://router.lan/admin", "https://files.example.evil.lan/x.mp4"):
        assert away.get("/api/p/tv1/play", params={"u": u}, follow_redirects=False).status_code == 404, u
        assert c.post("/api/p/tv1/jellyfin/video", json={"url": u, "title": "x"}).status_code == 404
    assert c.get("/api/relay", params={"u": "https://router.lan/other"}).status_code != 200  # never allowed
    assert not any("router" in f or "evil" in f for f in fetched)
    assert away.get("/api/p/tv1/play", params={"u": MOVIE}, follow_redirects=False).status_code == 200  # listed


def test_the_relay_checks_the_address_it_actually_connected_to(monkeypatch):
    # A stream server's name that passed the check, then pointed at this Mac when connecting (a short DNS lifetime).
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Here(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"this Mac's own app")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Here)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    looks = []
    real = hls_mod.inside

    def first_lookup_lies(host):  # the name's first answer: somewhere else; every later look is real
        looks.append(host)
        return len(looks) > 1 and real(host)

    monkeypatch.setattr(hls_mod, "inside", first_lookup_lies)
    try:
        relay = HlsRelay()
        url = relay.allow(f"http://127.0.0.1:{server.server_address[1]}/live.m3u8")
        with pytest.raises(HlsError, match="this Mac"):
            relay.open(url)
        assert looks == ["127.0.0.1", "127.0.0.1"]  # the name before connecting, the address after
    finally:
        server.shutdown()


def test_the_whole_catalogue_goes_into_jellyfin_a_batch_of_shows_at_a_time(home, cat, tmp_path, monkeypatch):
    media, air = tmp_path / "media", "http://air:8724"
    shows_root, movies_root = media / "tv1", media / "tv1-movies"
    jobs = Jobs(tmp_path / "mimir-flix.db", "mimir-flix")
    app_jobs.define(jobs)
    fake = FakeJellyfin()
    jf = Jellyfin("http://jf", "k", transport=httpx.MockTransport(fake.handler))

    # Off unless the provider's settings turn it on: nothing written, nothing asked of the provider.
    assert app_jobs.whole_catalogue(jobs, home, jf, media, air) == "no provider has the whole catalogue turned on"
    assert not media.exists() and cat.fetched == []
    settings.save({"providers": [{"id": "tv1", "name": "My TV", "kind": "fakecat", "mirror": True}]}, home.config)
    cat = home.get("tv1")[1]  # a changed settings entry makes the provider again

    # A show added by hand keeps its own hourly check and is not written again; another show also called "Dark"
    # gets a folder of its own.
    cat.pages[SECTIONS["Shows"]].append(folder("DARK", f"{SITE}/remake"))
    cat.pages[f"{SITE}/remake"] = [video("Episode 1", "https://files.example/remake/e1.mp4")]
    cat.pages[SECTIONS["Shows"]].append(folder("Mine", f"{SITE}/mine"))
    cat.pages[f"{SITE}/mine"] = [video("Episode 1", "https://files.example/mine/e1.mp4")]
    follow(shows_root, "Mine", f"{SITE}/mine")

    # A batch of two: the movie, Dark, and Other (its page is down: tried again later); the remake waits.
    t = time.time()
    done = mirror.mirror(cat, "tv1", shows_root, movies_root, air, per_run=2, now=t)
    assert done == {"movies": 1, "shows": 1, "episodes": 3, "failed": 1}
    assert (movies_root / "Heat (1995)/Heat (1995).strm").exists()
    assert (shows_root / "Dark/Season 02/Dark S02E01 - Truth.strm").exists()
    assert f"{SITE}/mine" not in cat.fetched
    # The job takes the rest (never written first), makes the libraries Jellyfin did not have, and says what is left.
    summary = app_jobs.whole_catalogue(jobs, home, jf, media, air)
    assert summary == "My TV: 0 movies, 1 shows (1 episodes), 1 failed, 1 shows waiting"
    assert (shows_root / "DARK (2)/Season 01/DARK (2) S01E01.strm").exists()
    assert fake.libs["My TV"]["Locations"] == [str(shows_root)] and fake.libs["My TV"]["CollectionType"] == "tvshows"

    # A mirrored show keeps its latest episodes only; favourited in Jellyfin, it is followed and gets them all.
    assert sorted(f.name for f in (shows_root / "Dark").rglob("*.strm")) == [
        "Dark S01E01.strm", "Dark S01E02 - Lies.strm", "Dark S02E01 - Truth.strm"
    ]
    remake2 = f"{SITE}/remake2"
    cat.pages[SECTIONS["Shows"]].append(folder("Long", remake2))
    cat.pages[remake2] = [video(f"Episode {n}", f"https://files.example/long/e{n}.mp4") for n in range(5, 0, -1)]
    monkeypatch.setattr(mirror, "EPISODES_KEPT", 2)
    mirror.mirror(cat, "tv1", shows_root, movies_root, air, now=t)
    assert sorted(f.name for f in (shows_root / "Long").rglob("*.strm")) == ["Long S01E04.strm", "Long S01E05.strm"]
    fake.favourites.add("series:Long")
    app_jobs.whole_catalogue(jobs, home, jf, media, air)
    assert followed(shows_root)["Long"] == remake2
    refresh_shows(cat, "tv1", shows_root, air)  # the hourly check brings back the older ones
    assert len(list((shows_root / "Long").rglob("*.strm"))) == 5

    # Same-titled movies get folders of their own, also when an older run gave them one (the last written won).
    other_heat = "https://files.example/movies/heat-1986.mkv"
    cat.extra_movies = [{"kind": "movie", "url": other_heat, "title": "Heat (1995)", "page": SECTIONS["Movies"]}]
    write_movie("tv1", other_heat, "Heat (1995)", movies_root, air)  # overwrote the first Heat's file
    mirror.save(movies_root, mirror.mirrored(movies_root) | {other_heat: {"name": "Heat (1995)", "at": t}})
    mirror.mirror(cat, "tv1", shows_root, movies_root, air, now=t)
    files = {f.parent.name: f.read_text() for f in movies_root.rglob("*.strm")}
    assert sorted(files) == ["Heat (1995)", "Heat (1995) (2)"]
    assert {u for u in (MOVIE, other_heat) for text in files.values() if quote(u, safe="") in text} == {MOVIE, other_heat}

    # A written show is read again after a week, so its new episodes arrive; before that it is left alone.
    cat.pages[S2] = [video("Episode 2. New", EP.format(s=2, e=2)), *cat.pages[S2]]
    week = mirror.REWRITE_AFTER
    assert mirror.mirror(cat, "tv1", shows_root, movies_root, air, now=t + week - 1)["shows"] == 0
    assert not (shows_root / "Dark/Season 02/Dark S02E02 - New.strm").exists()
    assert mirror.mirror(cat, "tv1", shows_root, movies_root, air, now=t + week + 1)["shows"] >= 1
    assert (shows_root / "Dark/Season 02/Dark S02E02 - New.strm").exists()
