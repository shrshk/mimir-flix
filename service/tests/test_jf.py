import json
import time

import httpx
from fastapi.testclient import TestClient

from mimir_flix.app import create_app
from mimir_flix.jellyfin import Jellyfin
from mimir_flix.jf_routes import router
from mimir_flix.kodi import Kodi
from mimir_flix.watchlist import Watchlist
from mimir_flix.resolver import start_resolver

MOVIE = "a" * 32
WEDDING = "b" * 32
HOME = "c" * 32
CLIP = "d" * 32
BYTES = bytes(range(256)) * 40


def item(id_, name, type_, container="mp4", video="h264", audio="aac", **kw):
    return {
        "Id": id_,
        "Name": name,
        "Type": type_,
        "IsFolder": type_ in ("CollectionFolder", "Folder"),
        "RunTimeTicks": 36_000_000_000 if type_ != "CollectionFolder" else None,
        "ImageTags": {"Primary": "x"},
        "MediaSources": [
            {
                "Id": id_,
                "Container": container,
                "MediaStreams": [{"Type": "Video", "Codec": video}, {"Type": "Audio", "Codec": audio}],
            }
        ],
        **kw,
    }


SHOW, EP1, EP2 = "3" * 32, "1" * 32, "2" * 32
FILM, LIKE_IT = "4" * 32, "5" * 32
ITEMS = {
    MOVIE: item(MOVIE, "Wedding Film 4K", "Video"),
    EP1: item(EP1, "Pilot", "Episode", SeriesId=SHOW, SeriesName="A Show", ParentIndexNumber=1, IndexNumber=1),
    EP2: item(EP2, "Second", "Episode", SeriesId=SHOW, SeriesName="A Show", ParentIndexNumber=1, IndexNumber=2),
    CLIP: item(CLIP, "Old clip", "Video", container="avi", video="mpeg4", audio="mp3"),
    HOME: item(HOME, "Home Videos", "CollectionFolder"),
    "9" * 32: item("9" * 32, "NDTV 24x7", "TvChannel"),  # a live channel from an IPTV provider
    FILM: item(
        FILM,
        "Kalki 2898 AD",
        "Movie",
        ProductionYear=2024,
        OfficialRating="UA",
        CommunityRating=7.46,
        Genres=["Science Fiction", "Action"],
        People=[
            {"Name": "Prabhas", "Type": "Actor"},
            {"Name": "Deepika Padukone", "Type": "Actor"},
            {"Name": "Nag Ashwin", "Type": "Director"},
            {"Name": "C. Ashwini Dutt", "Type": "Producer"},
        ],
        BackdropImageTags=["b"],
    ),
    LIKE_IT: item(LIKE_IT, "Salaar", "Movie", ProductionYear=2023),
}


EPISODES: list[dict] = []  # newest first, as Jellyfin sorts them
NEXT_UP: list[dict] | None = []  # None: Jellyfin fails that call
SESSIONS: list[dict] = []
PLAYED: list[tuple] = []
TV_APP = {"Id": "tv", "Client": "Jellyfin Android TV", "SupportsRemoteControl": True, "PlayableMediaTypes": ["Video"]}
USER_DATA: dict[str, dict] = {}  # the admin's data per item: position, played, favourite
REPORTS: list[tuple] = []  # session reports: (event, body)
PHONE_APP = {"Id": "phone", "Client": "Jellyfin iOS", "SupportsRemoteControl": True, "PlayableMediaTypes": ["Video"]}


def fake(request: httpx.Request) -> httpx.Response:
    p, q = request.url.path, request.url.params
    if p == "/Library/MediaFolders":
        return httpx.Response(
            200,
            json={
                "Items": [
                    item(WEDDING, "Wedding", "CollectionFolder"),
                    ITEMS[HOME],
                    item("e" * 32, "My IPTV", "CollectionFolder"),
                ]
            },
        )
    if p == f"/Items/{FILM}/Similar":
        return httpx.Response(200, json={"Items": [ITEMS[LIKE_IT], ITEMS["9" * 32]]})  # a channel is no movie
    if p.endswith("/Similar"):
        return httpx.Response(500, text="boom")
    if p == "/Items" and "Ids" in q:
        return httpx.Response(200, json={"Items": [ITEMS[q["Ids"]]] if q["Ids"] in ITEMS else []})
    if p == "/Items" and "ParentId" in q:
        return httpx.Response(200, json={"Items": [ITEMS[MOVIE], ITEMS[CLIP]] if q["ParentId"] == HOME else []})
    if p == "/UserItems/Resume":
        assert q["userId"] == "u1"
        return httpx.Response(
            200, json={"Items": [ITEMS[MOVIE] | {"UserData": {"PlaybackPositionTicks": 9_000_000_000}}]}
        )
    if p == "/Shows/NextUp":
        return httpx.Response(500, text="boom") if NEXT_UP is None else httpx.Response(200, json={"Items": NEXT_UP})
    if p == "/Items" and q.get("SortBy") == "DateCreated":
        return httpx.Response(200, json={"Items": EPISODES})
    if p == "/Items" and "SearchTerm" in q:
        return httpx.Response(
            200, json={"Items": [i for i in ITEMS.values() if q["SearchTerm"].lower() in i["Name"].lower()]}
        )
    if p == f"/Videos/{MOVIE}/stream":
        assert "Token=" in request.headers["Authorization"]
        rng = request.headers.get("Range")
        if rng:
            a, b = (int(x) for x in rng.split("=")[1].split("-"))
            return httpx.Response(
                206,
                content=BYTES[a : b + 1],
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {a}-{b}/{len(BYTES)}",
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(200, content=BYTES, headers={"Content-Type": "video/mp4"})
    if p == "/System/Info/Public":
        return httpx.Response(200, json={"Id": "srv1", "ServerName": "air"})
    if p == "/Users":
        return httpx.Response(
            200,
            json=[
                {"Id": "u2", "Policy": {"IsAdministrator": False}},
                {"Id": "u1", "Policy": {"IsAdministrator": True}},
            ],
        )
    if p.startswith("/Sessions/Playing"):
        REPORTS.append((p.removeprefix("/Sessions/Playing").strip("/"), json.loads(request.content)))
        return httpx.Response(204)
    user_item = p.split("/")[2] if p.count("/") >= 2 else ""
    if p.startswith(("/UserItems/", "/UserPlayedItems/", "/UserFavoriteItems/")):
        assert q["userId"] == "u1"
        if user_item not in ITEMS:
            return httpx.Response(404)
        data = USER_DATA.setdefault(user_item, {"PlaybackPositionTicks": 0, "Played": False, "PlayCount": 0})
        if p.endswith("/UserData") and request.method == "POST":
            data.update(json.loads(request.content))
        elif p.startswith("/UserPlayedItems/"):
            data.update(Played=True, PlayCount=data["PlayCount"] + 1, PlaybackPositionTicks=0)
        elif p.startswith("/UserFavoriteItems/"):
            data["IsFavorite"] = request.method == "POST"
        return httpx.Response(200, json=data)
    if p == "/Items" and q.get("Filters") == "IsFavorite":
        return httpx.Response(200, json={"Items": [ITEMS[i] for i, d in USER_DATA.items() if d.get("IsFavorite")]})
    if p == f"/Shows/{SHOW}/Episodes":
        eps = [ITEMS[EP1], ITEMS[EP2]]
        at = next(n for n, e in enumerate(eps) if e["Id"] == q["startItemId"])
        return httpx.Response(200, json={"Items": eps[at : at + int(q["limit"])]})
    if p == "/Sessions":
        assert q["ControllableByUserId"] == "u1"
        return httpx.Response(200, json=SESSIONS)
    if p.startswith("/Sessions/") and "/Playing" in p:
        session = next(x for x in SESSIONS if x["Id"] == p.split("/")[2])
        if p.endswith("/Playing/Stop"):
            session.pop("NowPlayingItem", None)
            return httpx.Response(204)
        PLAYED.append((session["Id"], dict(q)))
        # Like the TV app: "play" while something plays only stops it.
        if not session.pop("NowPlayingItem", None):
            session["NowPlayingItem"] = {"Id": q["itemIds"]}
        return httpx.Response(204)
    if p.endswith("/Images/Primary"):
        return httpx.Response(200, content=b"jpg", headers={"Content-Type": "image/jpeg"})
    if p == "/Auth/Keys":  # what the passthrough must never reach: every API key
        return httpx.Response(200, json={"Items": [{"AccessToken": "admin-key"}]})
    return httpx.Response(404)


def setup(tmp_path):
    USER_DATA.clear()
    REPORTS.clear()
    jf = Jellyfin("http://jf", "secret-key", transport=httpx.MockTransport(fake))
    sent = []
    kodi = Kodi(
        "http://tv",
        "pw",
        transport=httpx.MockTransport(
            lambda r: (
                sent.append(r.read().decode()) or httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "OK"})
            )
        ),
    )
    watchlist = Watchlist(tmp_path / "m.db")
    c = TestClient(
        create_app(
            jf,
            tmp_path / "picks",
            router(jf, kodi, "https://lan.example", watchlist=watchlist, hidden=lambda: {"My IPTV"}),
        )
    )
    return jf, c, sent


def test_browse_search_and_pick_how_to_play(tmp_path):
    _, c, _ = setup(tmp_path)
    root = c.get("/api/jf/browse").json()
    assert [i["title"] for i in root["items"]] == ["Wedding", "Home Videos"]  # a provider's library: in its own section
    home = c.get("/api/jf/browse", params={"id": HOME}).json()
    assert [(i["title"], i["kind"], i["sub"]) for i in home["items"]] == [
        ("Wedding Film 4K", "video", "60 min"),
        ("Old clip", "video", "60 min"),
    ]
    assert [i["title"] for i in c.get("/api/jf/search", params={"q": "wedding"}).json()] == ["Wedding Film 4K"]
    found = c.get("/api/jf/search", params={"q": "ndtv"}).json()
    assert [(i["title"], i["kind"], i["sub"]) for i in found] == [("NDTV 24x7", "video", "Live TV")]

    # Safari plays MP4/H.264 as is; an AVI/MPEG-4 file goes through Jellyfin's HLS conversion.
    src = c.get(f"/api/jf/play/{MOVIE}", follow_redirects=False)  # set by the page inside the tap, then sent on
    assert src.status_code == 302 and src.headers["location"].startswith(f"/jf/Videos/{MOVIE}/stream?static=true")
    assert "/master.m3u8?" in c.get(f"/api/jf/play/{CLIP}", follow_redirects=False).headers["location"]
    assert c.get(f"/api/jf/play/{HOME}").status_code == 400


def test_passthrough_streams_ranges_and_keeps_the_key_on_the_server(tmp_path):
    _, c, _ = setup(tmp_path)
    r = c.get(f"/jf/Videos/{MOVIE}/stream", params={"static": "true"}, headers={"Range": "bytes=100-199"})
    assert (
        r.status_code == 206
        and r.content == BYTES[100:200]
        and r.headers["content-range"] == f"bytes 100-199/{len(BYTES)}"
    )
    assert c.get(f"/jf/Items/{MOVIE}/Images/Primary").content == b"jpg"
    # Only videos and images pass through; nothing else of Jellyfin's API.
    assert c.get("/jf/Library/VirtualFolders").status_code == 404
    assert c.get("/jf/Users").status_code == 404
    # Nor by climbing out of an allowed path: dots (encoded or not) never reach Jellyfin, which would answer.
    for up in ("Videos/%2e%2e/Auth/Keys", "Videos/../Auth/Keys", f"Items/{MOVIE}/Images/%2e%2e/%2e%2e/%2e%2e/Auth/Keys",
               f"Videos/{MOVIE}/%2E%2E/%2E%2E/Auth/Keys", f"Videos/{MOVIE}/..%2f..%2fAuth/Keys"):
        r = c.get("/jf/" + up)
        assert r.status_code == 404 and b"admin-key" not in r.content, up


def test_ids_for_swiftfin_links(tmp_path):
    _, c, _ = setup(tmp_path)
    assert c.get("/api/jf/app").json() == {"server": "srv1", "user": "u1"}  # the administrator, not the first user


def test_tv_plays_in_jellyfins_tv_app_not_the_phone(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_jf.SESSIONS", [PHONE_APP, dict(TV_APP)])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    slept = []
    monkeypatch.setattr("mimir_flix.jellyfin.time.sleep", slept.append)
    _, c, sent = setup(tmp_path)
    assert c.post("/api/jf/tv", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert slept == []  # the app was already open: no waiting
    assert PLAYED == [("tv", {"playCommand": "PlayNow", "itemIds": MOVIE})]
    assert sent == []  # not Kodi


def test_tv_switches_from_what_it_is_playing_in_one_press(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_jf.SESSIONS", [TV_APP | {"NowPlayingItem": {"Id": CLIP}}])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    monkeypatch.setattr("mimir_flix.jellyfin.time.sleep", lambda s: None)
    _, c, _ = setup(tmp_path)
    assert c.post("/api/jf/tv", json={"id": MOVIE}).status_code == 200
    assert SESSIONS[0]["NowPlayingItem"] == {"Id": MOVIE}


def test_tv_opens_the_jellyfin_app_when_it_is_closed_and_waits_for_it(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_jf.SESSIONS", [PHONE_APP])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    opened = []

    def open_app():  # what the ADB call does: the app starts and connects to Jellyfin
        opened.append(1)
        SESSIONS.append(dict(TV_APP))

    jf = Jellyfin("http://jf", "secret-key", transport=httpx.MockTransport(fake))
    c = TestClient(create_app(jf, tmp_path / "picks", router(jf, None, "https://lan.example", open_app)))
    slept = []
    monkeypatch.setattr("mimir_flix.jellyfin.time.sleep", slept.append)
    assert c.post("/api/jf/tv", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert opened == [1] and [p[0] for p in PLAYED] == ["tv"]
    # The app connects before it is ready to play ("no playable items"): a moment to settle before the command.
    assert slept[-1] >= 2


def test_tv_asks_for_the_jellyfin_app_when_it_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_jf.SESSIONS", [PHONE_APP])  # never the phone
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    _, c, _ = setup(tmp_path)
    r = c.post("/api/jf/tv", json={"id": MOVIE})
    assert r.status_code == 502 and r.json()["detail"] == "Open Jellyfin on the TV first" and PLAYED == []


def test_jellyfin_on_the_iphone_starts_playing_in_swiftfin(tmp_path, monkeypatch):
    # Swiftfin's links only open an item's page; the page opens one, and the Air sends the app PlayNow once it is up.
    swiftfin = {"Id": "iphone", "Client": "Swiftfin iOS", "SupportsRemoteControl": True}
    idle = swiftfin | {"Id": "ipad", "LastActivityDate": "2000-01-01T00:00:00.0000000Z"}  # open somewhere, untouched
    sessions = [PHONE_APP, idle]
    monkeypatch.setattr("tests.test_jf.SESSIONS", sessions)
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    now = time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime())
    slept = []
    monkeypatch.setattr(
        "mimir_flix.jellyfin.time.sleep",
        lambda s: slept.append(s) or sessions.append(swiftfin | {"LastActivityDate": now}),
    )
    _, c, _ = setup(tmp_path)
    assert c.post("/api/jf/phone", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert len(slept) == 1  # waited for the app to come up
    assert PLAYED == [("iphone", {"playCommand": "PlayNow", "itemIds": MOVIE})]  # not the idle iPad, not the TV
    assert c.post("/api/jf/phone", json={"id": HOME}).status_code == 400  # a folder only opens


def test_kodi_and_the_lan_stream_it_fetches(tmp_path):
    jf, c, sent = setup(tmp_path)
    assert c.post("/api/jf/kodi", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert f'"file":"https://lan.example/jf/{MOVIE}"' in sent[0] and "secret-key" not in sent[0]

    server = start_resolver(None, 0, "127.0.0.1", jf=jf)
    port = server.server_address[1]
    r = httpx.get(f"http://127.0.0.1:{port}/jf/{MOVIE}", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == BYTES[:10]
    assert httpx.get(f"http://127.0.0.1:{port}/jf/not-an-id").status_code == 404
    server.shutdown()


def test_home_rows_continue_watching_next_up_and_new_episodes(tmp_path, monkeypatch):
    ep = {"Type": "Episode", "SeriesName": "The Traitors (Telugu)", "SeriesId": "s1", "SeriesPrimaryImageTag": "t"}
    monkeypatch.setattr(
        "tests.test_jf.EPISODES",
        [
            ep
            | {
                "Id": "e7",
                "Name": "Episode 7",
                "ParentIndexNumber": 1,
                "IndexNumber": 7,
                "DateCreated": "2099-01-01T00:00:00",
            }
        ],
    )
    monkeypatch.setattr(
        "tests.test_jf.NEXT_UP",
        [
            ep
            | {"Id": "e5", "Name": "Episode 5", "ParentIndexNumber": 1, "IndexNumber": 5, "ImageTags": {"Primary": "x"}}
        ],
    )
    _, c, _ = setup(tmp_path)
    rows = c.get("/api/jf/home").json()
    assert [(i["title"], i["progress"]) for i in rows["resume"]] == [("Wedding Film 4K", 25)]
    assert [(i["title"], i["sub"]) for i in rows["next_up"]] == [("Episode 5", "The Traitors (Telugu) · S01E05")]
    assert (rows["next_up"][0]["show"], rows["next_up"][0]["show_title"]) == ("jf:s1", "The Traitors (Telugu)")
    new = rows["new"][0]
    assert (new["sub"], new["thumb"]) == (
        "The Traitors (Telugu) · S01E07",
        "/jf/Items/s1/Images/Primary?maxHeight=480&quality=85",
    )
    # One row failing leaves the others.
    monkeypatch.setattr("tests.test_jf.NEXT_UP", None)
    rows = c.get("/api/jf/home").json()
    assert rows["next_up"] == [] and len(rows["resume"]) == 1 and len(rows["new"]) == 1


def test_my_list_titles_in_the_library_are_jellyfin_favourites(tmp_path):
    _, c, _ = setup(tmp_path)
    assert c.post("/api/jf/list", json={"id": MOVIE}).json() == {"added": ["Wedding Film 4K"], "already": []}
    assert USER_DATA[MOVIE]["IsFavorite"]  # every Jellyfin app shows it, and the recommendations read it
    assert c.post("/api/jf/list", json={"id": MOVIE}).json() == {"added": [], "already": ["Wedding Film 4K"]}
    listed = c.get("/api/jf/list").json()
    assert [(i["title"], i["id"], i["kind"]) for i in listed] == [("Wedding Film 4K", MOVIE, "video")]
    assert listed[0]["thumb"].startswith("/jf/Items/")  # shown and played through mimir-flix, as anywhere else
    USER_DATA[EP1] = {"IsFavorite": True}  # made a favourite in a Jellyfin app: on My list too
    assert {i["id"] for i in c.get("/api/jf/list").json()} == {MOVIE, EP1}
    assert c.delete("/api/jf/list", params={"id": MOVIE}).status_code == 204
    assert not USER_DATA[MOVIE]["IsFavorite"]
    assert c.delete("/api/jf/list", params={"id": MOVIE}).status_code == 404
    assert c.delete("/api/jf/list", params={"id": "../Users"}).status_code == 422


def test_library_titles_on_the_old_list_become_favourites_once(tmp_path):
    _, c, _ = setup(tmp_path)
    old = Watchlist(tmp_path / "m.db")
    old.add_item(f"jf:{CLIP}", "jellyfin", {"title": "Old clip"})
    old.add_item(f"jf:{'f' * 32}", "jellyfin", {"title": "Since deleted"})
    old.add_title({"tmdb": "movie/1", "title": "Streaming", "year": "2020"})
    assert [i["id"] for i in c.get("/api/jf/list").json()] == [CLIP]
    assert USER_DATA[CLIP]["IsFavorite"]
    assert [i["url"] for i in old.items()] == ["tmdb:movie/1"]  # the other kinds stay on mimir-flix's list


def test_play_here_keeps_jellyfins_place_and_played_state(tmp_path):
    _, c, _ = setup(tmp_path)
    w = {"id": MOVIE, "session": "5" * 32}  # the movie is an hour long
    assert c.get(f"/api/jf/resume/{MOVIE}").json() == {"start": 0}
    assert c.post("/api/jf/playing", json=w | {"event": "start", "position": 0}).status_code == 204
    c.post("/api/jf/playing", json=w | {"event": "progress", "position": 60})  # under 5%: no place kept yet
    assert USER_DATA[MOVIE]["PlaybackPositionTicks"] == 0
    c.post("/api/jf/playing", json=w | {"event": "progress", "position": 600, "paused": True})
    assert USER_DATA[MOVIE]["PlaybackPositionTicks"] == 6_000_000_000 and USER_DATA[MOVIE]["LastPlayedDate"]
    assert c.get(f"/api/jf/resume/{MOVIE}").json() == {"start": 600}  # Play here, the TV and the phone go on from here
    assert [(e, b["PositionTicks"], b["IsPaused"]) for e, b in REPORTS] == [
        ("", 0, False),
        ("Progress", 600_000_000, False),
        ("Progress", 6_000_000_000, True),
    ]
    c.post("/api/jf/playing", json=w | {"event": "stopped", "position": 3400})  # 94%: watched
    data = USER_DATA[MOVIE]
    assert (data["Played"], data["PlayCount"], data["PlaybackPositionTicks"]) == (True, 1, 0)
    assert REPORTS[-1][0] == "Stopped"
    assert c.post("/api/jf/playing", json=w | {"id": "x", "event": "stopped", "position": 1}).status_code == 422


def test_play_here_goes_on_to_the_next_episode(tmp_path):
    _, c, _ = setup(tmp_path)
    nxt = c.get(f"/api/jf/next/{EP1}").json()
    assert (nxt["id"], nxt["title"], nxt["sub"]) == (EP2, "Second", "A Show · S01E02 · 60 min")
    assert c.get(f"/api/jf/next/{EP2}").json() == {}  # the last one
    assert c.get(f"/api/jf/next/{MOVIE}").json() == {}  # not an episode


def test_a_movie_page_has_its_details_and_the_librarys_titles_like_it(tmp_path):
    _, c, _ = setup(tmp_path)
    page = c.get("/api/jf/title", params={"id": FILM}).json()
    assert (page["title"], page["year"], page["minutes"], page["rating"], page["score"]) == (
        "Kalki 2898 AD",
        2024,
        60,
        "UA",
        7.5,
    )
    assert page["genres"] == ["Science Fiction", "Action"] and page["directors"] == ["Nag Ashwin"]
    assert page["cast"] == ["Prabhas", "Deepika Padukone"]  # actors only: the producer is not cast
    assert page["backdrop"] == f"/jf/Items/{FILM}/Images/Backdrop?maxWidth=1280&quality=85"
    assert [m["title"] for m in page["more"]] == ["Salaar"]
    # Jellyfin failing the titles like it still shows the page; an id that is not one is refused before Jellyfin.
    other = c.get("/api/jf/title", params={"id": LIKE_IT}).json()
    assert other["more"] == [] and other["backdrop"] == "" and other["cast"] == []
    assert c.get("/api/jf/title", params={"id": "../Users"}).status_code == 422
    assert c.get("/api/jf/title", params={"id": "6" * 32}).status_code in (404, 502)
