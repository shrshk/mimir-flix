import shlex
import time
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimir_flix.streaming import Streaming, pick, router
from mimir_flix.tv import Tv, TvError


def providers(*names, category="flatrate"):
    return {"results": {"US": {category: [{"provider_name": n} for n in names]}, "IN": {"flatrate": []}}}


TMDB = {
    "/3/search/multi": {
        "results": [
            {"media_type": "movie", "id": 1, "title": "RRR", "release_date": "2022-03-24", "poster_path": "/r.jpg"},
            {"media_type": "tv", "id": 2, "name": "The Office", "first_air_date": "2005-03-24", "overview": "Paper"},
            {"media_type": "person", "id": 3, "name": "N. T. Rama Rao Jr."},
            {"media_type": "movie", "id": 4, "title": "Only to rent"},
            {"media_type": "tv", "id": 5, "name": "Only on YouTube TV"},
        ]
    },
    "/3/movie/1/watch/providers": providers("Netflix Standard with Ads", "Paramount+ Amazon Channel"),
    "/3/tv/2/watch/providers": providers("Peacock Premium", "Hulu"),
    "/3/movie/4/watch/providers": providers("Apple TV", category="rent"),
    "/3/tv/5/watch/providers": providers("YouTube TV"),
    "/3/watch/providers/movie": {
        "results": [
            {"provider_id": 8, "provider_name": "Netflix"},
            {"provider_id": 386, "provider_name": "Peacock Premium"},
        ]
    },
    "/3/watch/providers/tv": {
        "results": [
            {"provider_id": 1796, "provider_name": "Netflix Standard with Ads"},
            {"provider_id": 15, "provider_name": "Hulu"},
        ]
    },
    "/3/discover/movie": {
        "results": [
            {"id": 7, "title": "Big Movie", "release_date": "2026-01-02", "popularity": 50, "poster_path": "/m.jpg"},
            {"id": 8, "title": "No Poster", "popularity": 99},
        ]
    },
    "/3/discover/tv": {
        "results": [
            {"id": 9, "name": "Hit Show", "first_air_date": "2025-05-05", "popularity": 80, "poster_path": "/s.jpg"},
        ]
    },
    "/3/trending/all/week": {
        "results": [
            {"media_type": "tv", "id": 5, "name": "Only on YouTube TV"},
            {
                "media_type": "tv",
                "id": 2,
                "name": "The Office",
                "first_air_date": "2005-03-24",
                "backdrop_path": "/o.jpg",
            },
        ]
    },
}


def jw(kind, tmdb_id, *offers):
    return {
        "node": {
            "objectType": kind,
            "content": {"externalIds": {"tmdbId": str(tmdb_id)}},
            "offers": [{"monetizationType": m, "package": {"clearName": n}, "standardWebURL": u} for m, n, u in offers],
        }
    }


JUSTWATCH = {
    "data": {
        "popularTitles": {
            "edges": [
                jw(
                    "MOVIE", 99, ("FLATRATE", "Netflix", "https://www.netflix.com/title/111")
                ),  # same name, another title
                jw(
                    "SHOW",
                    96677,
                    ("RENT", "Netflix", "https://www.netflix.com/title/222"),
                    ("FLATRATE", "Netflix", "https://evil.example/title/333"),  # not Netflix's own domain
                    ("FLATRATE", "Netflix", "https://www.netflix.com/title/80994082"),
                    ("FLATRATE", "Amazon Prime Video", "https://app.primevideo.com/detail?gti=amzn1"),
                ),
            ]
        }
    }
}


class FakeTmdb:
    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "apis.justwatch.com":
            assert "Authorization" not in request.headers and "api_key" not in str(request.url)  # no TMDB credential
            return httpx.Response(200, json=JUSTWATCH)
        bearer = request.headers.get("Authorization") == "Bearer eyJtok"
        if not bearer and request.url.params.get("api_key") != "k" * 32:
            return httpx.Response(401)
        return httpx.Response(200, json=TMDB[request.url.path]) if request.url.path in TMDB else httpx.Response(404)


def test_search_keeps_titles_on_the_houses_apps_and_says_which(tmp_path):
    tmdb = FakeTmdb()
    house = pick("netflix,prime,peacock,youtube")  # not Hulu
    s = Streaming("eyJtok", tmp_path / "m.db", house, transport=httpx.MockTransport(tmdb.handler))
    found = s.search("rrr")
    assert [(f["title"], f["year"], f["kind"], [a["name"] for a in f["apps"]]) for f in found] == [
        ("RRR", "2022", "movie", ["Netflix", "Prime Video"]),  # Paramount+ plays through Prime Video
        ("The Office", "2005", "show", ["Peacock"]),
    ]  # no person, nothing only to rent, YouTube TV is not YouTube, Hulu is not one of the house's apps
    assert found[0]["thumb"] == "https://image.tmdb.org/t/p/w342/r.jpg"
    assert found[0]["apps"][0]["web"] == "https://www.netflix.com/search?q=RRR"
    assert all("eyJtok" not in str(r.url) for r in tmdb.calls)  # a Read Access Token only ever goes in the header
    n = len(tmdb.calls)
    s.search("rrr")
    assert len(tmdb.calls) == n  # kept a day


def test_without_a_token_search_is_empty_and_a_refused_token_is_an_error(tmp_path):
    tmdb = FakeTmdb()
    assert Streaming("", tmp_path / "a.db", transport=httpx.MockTransport(tmdb.handler)).search("rrr") == []
    assert tmdb.calls == []
    app = FastAPI()
    app.include_router(router(Streaming("bad", tmp_path / "b.db", transport=httpx.MockTransport(tmdb.handler)), None))
    r = TestClient(app).get("/api/streaming/search", params={"q": "rrr"})
    assert r.status_code == 502 and "refused" in r.json()["detail"]


def test_open_starts_only_the_houses_apps_on_the_tv(tmp_path):
    opened = []

    def open_app(package, title, url=""):
        if title == "asleep":
            raise TvError("The TV is not answering")
        opened.append((package, title) if not url else (package, title, url))

    app = FastAPI()
    app.include_router(router(Streaming("eyJtok", tmp_path / "m.db"), open_app))
    c = TestClient(app)
    assert c.post("/api/streaming/open", json={"app": "peacock", "title": "The Office"}).status_code == 204
    assert opened == [("com.peacocktv.peacockandroid", "The Office")]  # no TMDB id: no title link
    assert c.post("/api/streaming/open", json={"app": "com.evil", "title": "x"}).status_code == 404
    assert c.post("/api/streaming/open", json={"app": "netflix", "title": "asleep"}).status_code == 502
    assert c.get("/api/streaming/status").json()["tv"] is True

    no_tv = FastAPI()
    no_tv.include_router(router(Streaming("eyJtok", tmp_path / "m.db"), None))
    assert TestClient(no_tv).post("/api/streaming/open", json={"app": "netflix"}).status_code == 409


def test_the_title_reaches_the_tvs_shell_as_one_quoted_word(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(Tv, "shell", lambda self, command: sent.append(command) or "")
    Tv("tv", tmp_path / "adbkey").open_app("com.netflix.ninja", "RRR'; reboot; echo '")
    search = sent[0].split("; ", 1)[1]
    assert shlex.split(search.split(" 2>&1")[0])[-1] == "RRR'; reboot; echo '"
    Tv("tv", tmp_path / "adbkey").open_app("com.sling")
    assert sent[1] == "monkey -p com.sling -c android.intent.category.LEANBACK_LAUNCHER 1"
    Tv("tv", tmp_path / "adbkey").open_app("com.netflix.ninja", "Lupin", "https://www.netflix.com/title/1?a=b&c=d")
    view = sent[2].split("; ", 1)[1].split(" 2>&1")[0]
    words = shlex.split(view)
    assert words[words.index("-d") + 1] == "https://www.netflix.com/title/1?a=b&c=d"
    # Netflix gets its own TV link launch: its screens start over on the link, no force-stop, no "Who's watching?"
    assert words[-5:] == ["-e", "source", "30", "-n", "com.netflix.ninja/.MainActivity"] and "force-stop" not in view
    Tv("tv", tmp_path / "adbkey").open_app("com.amazon.amazonvideo.livingroom", "Lupin", "https://www.amazon.com/x")
    other = sent[3].split("; ", 1)[1].split(" 2>&1")[0]
    assert shlex.split(other)[-2:] == ["-p", "com.amazon.amazonvideo.livingroom"] and "source" not in other


def test_streaming_apps_names_known_apps_in_the_houses_order():
    assert [a.name for a in pick(" peacock, netflix ")] == ["Peacock", "Netflix"]
    assert len(pick("")) > 10  # empty: every app
    with pytest.raises(SystemExit, match="hbo"):
        pick("netflix,hbo")


def test_a_v3_api_key_goes_in_the_request_but_not_in_the_cache(tmp_path):
    import sqlite3

    tmdb = FakeTmdb()
    s = Streaming("k" * 32, tmp_path / "m.db", transport=httpx.MockTransport(tmdb.handler))
    assert [f["title"] for f in s.search("rrr")] == ["RRR", "The Office"]
    assert all(r.url.params["api_key"] == "k" * 32 and "Authorization" not in r.headers for r in tmdb.calls)
    keys = [k for (k,) in sqlite3.connect(tmp_path / "m.db").execute("SELECT k FROM tmdb")]
    assert keys and not any("k" * 32 in k for k in keys)


def test_homes_trending_row_keeps_only_titles_on_the_houses_apps(tmp_path):
    tmdb = FakeTmdb()
    house = pick("netflix,peacock,youtube")
    s = Streaming("eyJtok", tmp_path / "m.db", house, transport=httpx.MockTransport(tmdb.handler))
    assert [(t["title"], [a["name"] for a in t["apps"]]) for t in s.trending()] == [("The Office", ["Peacock"])]
    assert s.trending()[0]["backdrop"] == "https://image.tmdb.org/t/p/w1280/o.jpg"  # Home's featured title
    assert Streaming("", tmp_path / "n.db").trending() == []


def test_suggestions_are_titles_from_one_search_without_app_lookups(tmp_path):
    tmdb = FakeTmdb()
    s = Streaming("eyJtok", tmp_path / "m.db", transport=httpx.MockTransport(tmdb.handler))
    assert s.suggest("r") == []
    assert s.suggest("rrr") == ["RRR", "The Office", "Only to rent", "Only on YouTube TV"]  # no person
    assert [r.url.path for r in tmdb.calls] == ["/3/search/multi"]  # nothing per title


def test_an_app_opens_at_the_title_itself_when_justwatch_has_its_link(tmp_path):
    from fastapi import FastAPI

    from mimir_flix.streaming import APPS

    tmdb = FakeTmdb()
    s = Streaming(
        "eyJtok", tmp_path / "m.db", pick("netflix,prime,peacock"), transport=httpx.MockTransport(tmdb.handler)
    )
    netflix, prime, peacock = (next(a for a in APPS if a.key == k) for k in ("netflix", "prime", "peacock"))
    assert s.title_link(netflix, "tv/96677", "Lupin") == "https://www.netflix.com/title/80994082"
    assert s.title_link(prime, "tv/96677", "Lupin") == "https://app.primevideo.com/detail?gti=amzn1"
    assert s.title_link(peacock, "tv/96677", "Lupin") == ""  # no offer: the app opens at its search instead
    assert s.title_link(netflix, "movie/96677", "Lupin") == ""  # the id must match, and the kind
    assert s.title_link(netflix, "x/../1", "Lupin") == ""
    assert len([c for c in tmdb.calls if c.url.host == "apis.justwatch.com"]) == 1  # kept a day

    opened = []
    app = FastAPI()
    app.include_router(router(s, lambda pkg, title, url: opened.append((pkg, title, url))))
    c = TestClient(app)
    assert (
        c.post("/api/streaming/open", json={"app": "netflix", "title": "Lupin", "tmdb": "tv/96677"}).status_code == 204
    )
    assert opened == [("com.netflix.ninja", "Lupin", "https://www.netflix.com/title/80994082")]
    go = c.get(
        "/api/streaming/go", params={"app": "netflix", "title": "Lupin", "tmdb": "tv/96677"}, follow_redirects=False
    )
    assert go.status_code == 302 and go.headers["location"] == "https://www.netflix.com/title/80994082"
    search = c.get("/api/streaming/go", params={"app": "netflix", "title": "Lupin"}, follow_redirects=False)
    assert search.headers["location"] == "https://www.netflix.com/search?q=Lupin"


def test_homes_app_rows_are_each_apps_popular_titles(tmp_path):
    tmdb = FakeTmdb()
    s = Streaming(
        "eyJtok", tmp_path / "m.db", pick("netflix,peacock,sling"), transport=httpx.MockTransport(tmdb.handler)
    )
    rows = s.rows(at_least=1, today=date(2026, 10, 31))
    assert [
        (r["name"], [(i["title"], i["kind"], [a["name"] for a in i["apps"]]) for i in r["items"]]) for r in rows
    ] == [
        ("Netflix", [("Hit Show", "show", ["Netflix"]), ("Big Movie", "movie", ["Netflix"])]),  # by popularity
        ("Peacock", [("Hit Show", "show", ["Peacock"]), ("Big Movie", "movie", ["Peacock"])]),
    ]  # Sling has no provider id here: no row; a title without a poster is left out
    netflix = [c for c in tmdb.calls if c.url.path == "/3/discover/movie"][0].url.params
    assert netflix["with_watch_providers"] == "8|1796" and netflix["watch_region"] == "US"
    assert netflix["primary_release_date.gte"] == "2024-10-31" and netflix["vote_count.gte"] == "50"  # what's current
    assert s.rows(at_least=3) == []  # thin rows are left out
    n = len(tmdb.calls)
    s.rows(at_least=1, today=date(2026, 10, 31))
    assert len(tmdb.calls) == n  # kept a day


def test_an_on_my_apps_title_opens_its_app_only_when_the_tv_plays_it(tmp_path):
    opened = []
    app = FastAPI()
    tv_only = lambda request: request.headers.get("x-forwarded-for") == "192.0.2.25"  # noqa: E731
    app.include_router(router(Streaming("eyJtok", tmp_path / "m.db"), lambda *a: opened.append(a), tv_only))
    c = TestClient(app)
    r = c.get("/api/streaming/launch", params={"app": "peacock", "tmdb": "tv/2", "title": "The Office"})
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and len(r.content) > 1000
    assert opened == []  # Jellyfin reading the file (or anyone else): the clip only, nothing opens
    tv = c.get(
        "/api/streaming/launch",
        params={"app": "peacock", "title": "The Office"},
        headers={"x-forwarded-for": "192.0.2.25"},
    )
    assert tv.status_code == 200
    for _ in range(50):
        if opened:
            break
        time.sleep(0.02)
    assert opened == [("com.peacocktv.peacockandroid", "The Office", "")]
    assert c.get("/api/streaming/launch", params={"app": "com.evil"}).status_code == 404


def test_an_app_with_another_package_on_some_tvs_opens_the_installed_one(tmp_path, monkeypatch):
    sent = []

    def shell(self, command):
        sent.append(command)
        return "com.apple.atve.sony.appletv\n" if command.startswith("pm path") else ""

    monkeypatch.setattr(Tv, "shell", shell)
    apple = next(a for a in pick("appletv"))
    Tv("tv", tmp_path / "adbkey").open_app(apple.packages)
    assert sent[-1] == "monkey -p com.apple.atve.sony.appletv -c android.intent.category.LEANBACK_LAUNCHER 1"


def test_on_my_apps_takes_each_apps_whole_catalogue_page_by_page_up_to_a_cap(tmp_path):
    calls = []

    def movie(i, pop, poster=True):
        return {"id": i, "title": f"M{i}", "popularity": pop, "poster_path": f"/{i}.jpg" if poster else None}

    def handler(request):
        calls.append(request.url)
        p, q = request.url.path, request.url.params
        if p.startswith("/3/watch/providers/"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"provider_id": 8, "provider_name": "Netflix"},
                        {"provider_id": 386, "provider_name": "Peacock"},
                    ]
                },
            )
        if p == "/3/trending/all/week":
            return httpx.Response(200, json={"results": []})
        assert q["vote_count.gte"] == "5" and q["sort_by"] == "popularity.desc"
        netflix, page = q["with_watch_providers"] == "8", q["page"]
        if p == "/3/discover/movie" and netflix:
            pages = {"1": [movie(1, 50), movie(2, 10)], "2": [movie(3, 30, poster=False)]}
            return httpx.Response(200, json={"results": pages[page], "total_pages": 2})
        if p == "/3/discover/tv" and netflix:
            show = {"id": 4, "name": "S4", "popularity": 40, "poster_path": "/4.jpg"}
            return httpx.Response(200, json={"results": [show], "total_pages": 1})
        if p == "/3/discover/movie":
            return httpx.Response(200, json={"results": [movie(1, 50)], "total_pages": 1})
        return httpx.Response(200, json={"results": [], "total_pages": 1})

    s = Streaming("eyJtok", tmp_path / "m.db", pick("netflix,peacock"), transport=httpx.MockTransport(handler))
    titles = s.catalog(per_app=10)
    # Every page read, most popular first, a title without a poster left out; on two apps, it says both.
    assert [(t["title"], [a["name"] for a in t["apps"]]) for t in titles] == [
        ("M1", ["Netflix", "Peacock"]),
        ("S4", ["Netflix"]),
        ("M2", ["Netflix"]),
    ]
    assert any(u.path == "/3/discover/movie" and u.params["page"] == "2" for u in calls)
    # The cap stops the paging early.
    calls.clear()
    netflix = pick("netflix")[0]
    assert [t["title"] for t in s.on_app(netflix, 1, [8])] == ["M1"]
    assert not any(u.params.get("page") == "2" for u in calls)
