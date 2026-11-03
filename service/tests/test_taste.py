import json
import sqlite3

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimir_flix.jellyfin import Jellyfin
from mimir_flix.mcp_server import router as mcp_router
from mimir_flix.recs import Recs
from mimir_flix.recs import router as recs_router
from mimir_flix.streaming import Streaming, pick
from mimir_flix.taste import Taste
from mimir_flix.taste import router as taste_router
from mimir_flix.watchlist import Watchlist
from mimir_flix.watchlist import router as list_router


def hit(i, title, lang, genres, votes=500):
    return {
        "id": i,
        "title": title,
        "original_language": lang,
        "genre_ids": genres,
        "vote_average": 7.6,
        "vote_count": votes,
        "popularity": 50,
        "release_date": "2026-09-01",
        "poster_path": f"/{i}.jpg",
        "overview": f"About {title}.",
    }


TANDEL, RRR = hit(1, "Tandel", "te", [18]), hit(2, "RRR", "te", [28, 18])
SCARY, KDRAMA = hit(4, "Scary", "en", [27]), hit(5, "Seoul Nights", "ko", [18])
N1899 = hit(1399, "", "de", [18]) | {"name": "1899", "first_air_date": "2022-11-17"}


def tmdb(request: httpx.Request) -> httpx.Response:
    p = request.url.path.removeprefix("/3")
    if p.startswith("/watch/providers/"):
        return httpx.Response(200, json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]})
    if p == "/genre/movie/list":
        genres = [{"id": 18, "name": "Drama"}, {"id": 27, "name": "Horror"}, {"id": 28, "name": "Action"}]
        return httpx.Response(200, json={"genres": genres})
    if p == "/genre/tv/list":
        return httpx.Response(200, json={"genres": [{"id": 18, "name": "Drama"}]})
    if p.startswith("/discover/"):
        return httpx.Response(200, json={"results": [TANDEL, RRR, SCARY, KDRAMA] if p.endswith("movie") else []})
    if p.endswith("/watch/providers"):
        return httpx.Response(200, json={"results": {"US": {"flatrate": [{"provider_name": "Netflix"}]}}})
    if p == "/search/movie":
        known = {"Tandel": TANDEL, "RRR": RRR, "Thin": hit(7, "Thin", "te", [18], votes=3)}
        found = known.get(request.url.params.get("query"))
        return httpx.Response(200, json={"results": [found] if found else []})
    if p == "/tv/70523/recommendations":
        return httpx.Response(200, json={"results": [N1899]})
    if p.endswith("/recommendations") or p.endswith("/similar"):
        return httpx.Response(200, json={"results": []})
    known = {"/movie/1": TANDEL, "/movie/2": RRR, "/tv/70523": {"id": 70523, "name": "Dark"}, "/tv/1399": N1899}
    if p in known:
        h = known[p]
        return httpx.Response(200, json=h | {"genres": [{"id": g, "name": "Drama"} for g in h.get("genre_ids", [])]})
    return httpx.Response(404, json={})


def jellyfin(request: httpx.Request) -> httpx.Response:
    """The house watched RRR (a favourite) and three episodes of Dark; a home video never comes up."""
    q = dict(request.url.params)
    if request.url.path == "/Users":
        return httpx.Response(200, json=[{"Id": "u1", "Policy": {"IsAdministrator": True}}])
    rrr = {
        "Id": "m2",
        "Name": "RRR",
        "Type": "Movie",
        "Genres": ["Action", "Drama"],
        "ProviderIds": {"Tmdb": "2"},
        "People": [{"Name": "S. S. Rajamouli", "Type": "Director"}],
        "UserData": {"IsFavorite": True, "LastPlayedDate": "2026-10-20T20:00:00Z"},
    }
    dark = {
        "Id": "s1",
        "Name": "Dark",
        "Type": "Series",
        "Genres": ["Sci-Fi & Fantasy"],
        "ProviderIds": {"Tmdb": "70523"},
    }
    if q.get("Filters") == "IsPlayed" and q.get("IncludeItemTypes") == "Movie":
        return httpx.Response(200, json={"Items": [rrr]})
    if q.get("Filters") == "IsPlayed" and q.get("IncludeItemTypes") == "Episode":
        eps = [{"SeriesId": "s1", "UserData": {"LastPlayedDate": f"2026-10-2{n}T21:00:00Z"}} for n in range(1, 4)]
        return httpx.Response(200, json={"Items": eps})
    if q.get("Ids") == "s1":
        return httpx.Response(200, json={"Items": [dark]})
    if q.get("Filters") == "IsFavorite":
        return httpx.Response(200, json={"Items": [rrr]})
    return httpx.Response(200, json={"Items": []})


def setup(tmp_path, rules=None):
    db = tmp_path / "m.db"
    s = Streaming("eyJtok", db, pick("netflix"), transport=httpx.MockTransport(tmdb))
    jf = Jellyfin("http://jf", "key", transport=httpx.MockTransport(jellyfin))
    taste = None
    recs = Recs(s, lambda: taste.played_titles(), rules=lambda: rules or {}, disliked=lambda: taste.disliked())
    taste = Taste(db, recs, jf)
    taste.played_titles = lambda: {p["title"].lower() for p in taste.played()}
    app = FastAPI()
    for r in (recs_router(recs), taste_router(taste), list_router(Watchlist(db), recs.details), mcp_router("t")):
        app.include_router(r)
    return TestClient(app)


def tool(c, name, **args):
    r = c.post(
        "/mcp",
        headers={"Authorization": "Bearer t"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
    ).json()["result"]
    text = r["content"][0]["text"]
    return (text, True) if r["isError"] else json.loads(text)


def test_an_assistant_reads_the_taste_and_ranks_the_pool(tmp_path):
    c = setup(tmp_path, rules={"avoid_genres": ["horror"], "languages": ["te", "de", "en"]})

    taste = tool(c, "taste")
    # RRR's genres weigh 3 (watched, and a favourite); Dark's 1.6 (a show, three episodes)
    assert {(g["name"], g["weight"]) for g in taste["genres"]} == {
        ("Action", 3),
        ("Drama", 3),
        ("Sci-Fi & Fantasy", 1.6),
    }
    assert [x["title"] for x in taste["liked"]] == ["RRR", "Dark"]  # favourite first, then a show watched far
    assert taste["rules"]["avoid_genres"] == ["horror"]
    assert tool(c, "history")[0]["title"] == "Dark"  # most recent first

    pool = {i["title"]: i for i in tool(c, "candidates")}
    # Watched (RRR), the avoided genre (Scary: horror) and a language not kept (Korean) are left out.
    assert set(pool) == {"Tandel", "1899"}
    assert pool["1899"]["reasons"] == ["Like Dark; 7.6/10 from 500 votes."] and pool["Tandel"]["genres"] == ["Drama"]
    assert "thumb" not in pool["Tandel"] and pool["Tandel"]["apps"] == ["netflix"]  # lean for the model

    # Thinly rated or old titles can be left out: Tandel (Telugu, 500 votes) clears 1000/10; 1899 (German) does not.
    assert [i["title"] for i in tool(c, "candidates", min_votes=1000)] == ["Tandel"]
    assert tool(c, "candidates", year_from=2027) == "No candidates: is the TMDB token set up (Settings)?"

    # 👎 in a chat: left out from now on.
    assert tool(c, "feedback", tmdb="tv/1399", vote="down", title="1899") == {"recorded": "down", "tmdb": "tv/1399"}
    assert [i["title"] for i in tool(c, "candidates")] == ["Tandel"]


def test_the_chosen_recommendations_go_to_my_list_in_one_go(tmp_path):
    c = setup(tmp_path)
    added = tool(c, "add_to_my_list", tmdb_ids=["movie/1", "tv/1399"])
    assert added == {"added": ["Tandel", "1899"], "already": []}
    assert tool(c, "add_to_my_list", tmdb_ids=["movie/1"]) == {"added": [], "already": ["Tandel"]}
    listed = tool(c, "my_list")["titles"]
    assert {(t["title"], t["kind"], tuple(t["apps"])) for t in listed} == {
        ("Tandel", "movie", ("netflix",)),
        ("1899", "show", ("netflix",)),
    }
    assert tool(c, "remove_from_my_list", tmdb="tv/1399") == {"removed": "tv/1399"}
    assert [t["title"] for t in tool(c, "my_list")["titles"]] == ["Tandel"]
    assert tool(c, "add_to_my_list", tmdb_ids=["movie/999"])[1] is True  # TMDB has no such title


def test_my_list_takes_the_names_a_model_guesses(tmp_path):
    # 2026-11-02: a model called add_to_my_list with tmdb="movie/…" (not tmdb_ids) and gave up on the error.
    c = setup(tmp_path)
    assert tool(c, "add_to_my_list", tmdb="movie/1", title="Tandel", app="netflix") == {
        "added": ["Tandel"],
        "already": [],
    }
    assert tool(c, "remove_from_my_list", tmdb_ids=["movie/1"]) == {"removed": "movie/1"}
    text, failed = tool(c, "add_to_my_list", title="Tandel")
    assert failed and "tmdb_ids" in text and "provider's id" in text  # the error names the arguments to use


def test_a_list_from_before_streaming_titles_keeps_its_entries(tmp_path):
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE watchlist (url TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, thumb TEXT NOT NULL"
        " DEFAULT '', sub TEXT NOT NULL DEFAULT '', page TEXT NOT NULL DEFAULT '', here INTEGER NOT NULL DEFAULT 0,"
        " added REAL NOT NULL, episodes TEXT, new INTEGER NOT NULL DEFAULT 0)"
    )
    old.execute("INSERT INTO watchlist (url, kind, title, added) VALUES ('https://x/show', 'show', 'Old show', 1)")
    old.commit()
    w = Watchlist(db)
    assert w.add_title({"tmdb": "movie/1", "title": "Tandel", "thumb": "", "year": "2026"}) is True
    assert [i["title"] for i in w.items()] == ["Tandel", "Old show"] and w.titles()[0]["tmdb"] == "movie/1"


def test_reviewed_titles_are_found_in_one_call_and_the_pool_leaves_thin_ones_out(tmp_path):
    c = setup(tmp_path)
    said = [{"title": "Tandel", "year": 2026}, {"title": "RRR", "year": 2022}, {"title": "Nowhere", "year": 2026}]
    found = tool(c, "find_titles", titles=said + [{"title": "Thin", "year": 2026}])
    assert [i["title"] for i in found["items"]] == ["Tandel", "Thin"]  # a review can lift a thinly rated title
    assert found["not_found"] == ["Nowhere"] and found["left_out"] == ["RRR"]  # RRR was watched
    assert found["items"][0]["apps"] == ["netflix"] and found["items"][0]["tmdb"] == "movie/1"
