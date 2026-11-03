from datetime import date

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimir_flix.recs import Recs, router
from mimir_flix.streaming import Streaming, pick

TODAY = date(2026, 11, 1)
FLAT = {"flatrate": [{"provider_name": "Netflix"}]}
PRIME = {"flatrate": [{"provider_name": "Amazon Prime Video"}]}
MOVIES = [
    {
        "id": 1,
        "title": "Tandel",
        "original_language": "te",
        "vote_average": 7.6,
        "vote_count": 60,
        "popularity": 90,
        "release_date": "2026-09-20",
        "poster_path": "/t.jpg",
        "overview": "A fisherman's story.",
    },
    {
        "id": 2,
        "title": "RRR",
        "original_language": "te",
        "vote_average": 7.8,
        "vote_count": 900,
        "popularity": 40,
        "release_date": "2022-03-24",
        "poster_path": "/r.jpg",
    },
    {
        "id": 3,
        "title": "No Poster",
        "original_language": "te",
        "vote_average": 8.0,
        "vote_count": 99,
        "popularity": 99,
        "release_date": "2026-10-01",
        "poster_path": None,
    },
]


class FakeTmdb:
    def __init__(self):
        self.discover = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, dict(request.url.params)
        if p == "/3/watch/providers/movie" or p == "/3/watch/providers/tv":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"provider_id": 8, "provider_name": "Netflix"},
                        {"provider_id": 9, "provider_name": "Amazon Prime Video"},
                        {"provider_id": 15, "provider_name": "Hulu"},
                    ]
                },
            )
        if p == "/3/genre/movie/list":
            return httpx.Response(200, json={"genres": [{"id": 35, "name": "Comedy"}, {"id": 18, "name": "Drama"}]})
        if p == "/3/genre/tv/list":
            return httpx.Response(200, json={"genres": [{"id": 18, "name": "Drama"}]})
        if p.startswith("/3/discover/"):
            self.discover.append((p.rsplit("/", 1)[1], q))
            return httpx.Response(200, json={"results": MOVIES if p.endswith("movie") else []})
        if p.endswith("/watch/providers"):
            tid = p.split("/")[3]
            return httpx.Response(200, json={"results": {"US": PRIME if tid == "2" else FLAT}})
        return httpx.Response(404)


def setup(tmp_path, seen=()):
    fake = FakeTmdb()
    s = Streaming("eyJtok", tmp_path / "m.db", pick("netflix,prime"), transport=httpx.MockTransport(fake.handler))
    return Recs(s, lambda: set(seen)), fake


def test_filters_go_to_tmdb_and_each_pick_says_why_and_where(tmp_path):
    recs, fake = setup(tmp_path)
    out = recs.recommend("worth_the_hype", language="te", genre="Comedy", kind="movie", today=TODAY)
    kind, q = fake.discover[-1]
    assert kind == "movie" and q["with_original_language"] == "te" and q["with_genres"] == "35"
    assert q["vote_average.gte"] == "7.0" and q["vote_count.gte"] == "25"  # Indian titles: a lower vote floor
    assert q["with_watch_providers"] == "8|9" and q["primary_release_date.gte"] == "2026-07-04"  # the house's apps
    assert [(i["title"], [a["app"] for a in i["apps"]]) for i in out["items"]] == [
        ("Tandel", ["netflix"]),
        ("RRR", ["prime"]),
    ]
    assert out["items"][0]["why"] == "Popular right now and well rated: 7.6/10 from 60 votes."  # no poster: left out


def test_the_person_picks_the_apps_and_what_was_watched_is_left_out(tmp_path):
    recs, fake = setup(tmp_path, seen={"rrr"})
    out = recs.recommend("crowd_favourites", apps="netflix", kind="movie", min_rating=7.9, today=TODAY)
    q = fake.discover[-1][1]
    assert q["with_watch_providers"] == "8" and q["vote_average.gte"] == "7.9"  # stricter than the category's 7.5
    assert "primary_release_date.gte" not in q  # any year
    assert [i["title"] for i in out["items"]] == ["Tandel"]  # RRR watched (and on Prime only)
    recs.recommend("hidden_gems", apps="any", kind="movie", today=TODAY)
    q = fake.discover[-1][1]
    assert "with_watch_providers" not in q and q["vote_count.lte"] == "1500"  # every app; few votes


def test_bad_choices_are_explained(tmp_path):
    app = FastAPI()
    recs, _ = setup(tmp_path)
    app.include_router(router(recs))
    c = TestClient(app)
    assert "unknown category" in c.get("/api/recs", params={"category": "best"}).json()["detail"]
    assert "unknown apps" in c.get("/api/recs", params={"apps": "hbo-go"}).json()["detail"]
    opts = c.get("/api/recs/options").json()
    assert opts["categories"]["hidden_gems"] == "Hidden gems" and "drama" in opts["genres"]
    assert [a["key"] for a in opts["apps"]] == ["netflix", "prime"]
