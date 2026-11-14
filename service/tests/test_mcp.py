import json
import time

import httpx
from fastapi.testclient import TestClient

from mimir_tv.app import create_app, mcp_token
from mimir_tv.jellyfin import Jellyfin
from mimir_tv.jf_routes import router as jf_router
from mimir_tv.mcp_server import router
from mimir_tv.streaming import Streaming, pick
from mimir_tv.streaming import router as streaming_router
from mimir_tv.tv import Tv, TvError
from tests.test_jf import MOVIE, TV_APP, fake
from tests.test_streaming import FakeTmdb

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def setup(tmp_path, tv_now=None, opened=None, providers=()):
    jf = Jellyfin("http://jf", "secret-key", transport=httpx.MockTransport(fake))
    streaming = Streaming(
        "eyJtok", tmp_path / "m.db", pick("netflix,prime,peacock"), transport=httpx.MockTransport(FakeTmdb().handler)
    )
    open_app = (lambda package, title, url="": opened.append((package, title))) if opened is not None else None
    app = create_app(
        jf,
        tmp_path / "picks",
        jf_router(jf, None, "https://lan.example"),
        streaming_router(streaming, open_app),
        router(TOKEN, tv_now),
        connectors={"providers": list(providers)},
        hosts=[],  # as when running: only its own names (the tools call the app in-process by one)
    )
    return TestClient(app, client=("127.0.0.1", 5000), base_url="http://127.0.0.1:8723")  # OpenClaw on the Air


def rpc(c, method, params=None, id_=1, headers=AUTH):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}, headers=headers)


def tool(c, name, **args):
    r = rpc(c, "tools/call", {"name": name, "arguments": args}).json()["result"]
    text = r["content"][0]["text"]
    return (text, True) if r["isError"] else (json.loads(text), False)


def test_only_the_token_holder_on_the_air_or_the_tailnet_gets_in(tmp_path):
    c = setup(tmp_path)
    assert rpc(c, "ping", headers={}).status_code == 401
    assert rpc(c, "ping", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert rpc(c, "ping").json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    lan = AUTH | {"X-Forwarded-For": "192.0.2.25"}  # the TV, through Caddy: the token alone is not enough
    assert rpc(c, "ping", headers=lan).status_code == 403
    assert rpc(c, "ping", headers=AUTH | {"X-Forwarded-For": "100.64.0.2"}).status_code == 200


def test_a_client_starts_up_and_lists_the_tools(tmp_path):
    c = setup(tmp_path)
    init = rpc(c, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}).json()["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["capabilities"] == {"tools": {}}
    assert rpc(c, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"] == "2025-06-18"
    note = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=AUTH)
    assert note.status_code == 202 and note.content == b""
    names = [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]]
    assert {"search", "play_on_tv", "whats_on_tv"} <= set(names)
    assert rpc(c, "nope").json()["error"]["code"] == -32601
    assert rpc(c, "tools/call", {"name": "rm_rf"}).json()["error"]["code"] == -32602


def test_search_covers_the_library_and_the_apps_and_skips_providers_when_there_are_none(tmp_path):
    c = setup(tmp_path)
    found, failed = tool(c, "search", query="wedding")
    assert not failed
    assert [(i["jellyfin_id"], i["title"]) for i in found["library"]] == [(MOVIE, "Wedding Film 4K")]
    assert "providers" not in found and "unavailable" not in found
    found, _ = tool(c, "search", query="rrr")
    assert [(t["title"], t["tmdb"], t["apps"]) for t in found["apps"]] == [
        ("RRR", "movie/1", ["netflix", "prime"]),
        ("The Office", "tv/2", ["peacock"]),
    ]
    assert tool(c, "search", query="x") == ("query needs at least 2 characters", True)
    assert tool(c, "search") == ("missing query", True)


def test_play_on_tv_starts_jellyfin_or_opens_an_app_and_explains_what_went_wrong(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_jf.SESSIONS", [dict(TV_APP)])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    opened = []
    c = setup(tmp_path, opened=opened)
    assert tool(c, "play_on_tv", jellyfin_id=MOVIE) == ({"playing": "Wedding Film 4K"}, False)
    from tests.test_jf import PLAYED

    assert PLAYED == [("tv", {"playCommand": "PlayNow", "itemIds": MOVIE})]
    assert tool(c, "play_on_tv", app="peacock", title="The Office") == (
        {"opened": "peacock", "title": "The Office"},
        False,
    )
    assert opened == [("com.peacocktv.peacockandroid", "The Office")]
    assert tool(c, "play_on_tv", app="com.evil", title="x") == ("not one of the house's apps", True)
    assert tool(c, "play_on_tv", jellyfin_id=MOVIE, app="netflix")[1] is True  # one at a time
    assert tool(c, "play_on_tv", jellyfin_id=MOVIE, via="kodi") == ("Kodi is not set up", True)


def test_whats_on_tv_names_the_app_in_front(tmp_path, monkeypatch):
    out = (
        "  mWakefulness=Awake\n"
        "    mResumedActivity: ActivityRecord{6c1f2a u0 com.google.android.youtube.tv/"
        "com.google.android.apps.youtube.tv.activity.ShellActivity t512}\n"
    )
    monkeypatch.setattr(Tv, "shell", lambda self, command: out)
    c = setup(tmp_path, tv_now=Tv("tv", tmp_path / "adbkey").now)
    assert tool(c, "whats_on_tv") == (
        {"screen_on": True, "app": "com.google.android.youtube.tv", "app_name": "YouTube"},
        False,
    )

    def asleep():
        raise TvError("The TV is not answering")

    assert tool(setup(tmp_path, tv_now=asleep), "whats_on_tv") == ("The TV is not answering", True)
    assert tool(setup(tmp_path), "whats_on_tv") == ("No TV set up (add one on the Settings page)", True)


def test_the_token_is_made_once_and_only_the_owner_can_read_it(tmp_path):
    path = tmp_path / "mcp-token"
    first = mcp_token(path)
    assert len(first) >= 40 and mcp_token(path) == first
    assert path.stat().st_mode & 0o777 == 0o600


def test_whats_new_lists_episodes_the_library_added_lately_and_leaves_out_older_ones(tmp_path, monkeypatch):
    day = 86400
    stamp = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime(time.time() - ago))  # noqa: E731
    episode = {"Type": "Episode", "SeriesName": "Koffee with Karan", "SeriesId": "k1", "ParentIndexNumber": 9}
    monkeypatch.setattr(
        "tests.test_jf.EPISODES",
        [
            episode | {"Id": "e3", "Name": "Vijay and Rashmika", "IndexNumber": 3, "DateCreated": stamp(day)},
            episode | {"Id": "e2", "Name": "Older", "IndexNumber": 2, "DateCreated": stamp(9 * day)},
        ],
    )
    monkeypatch.setattr("tests.test_jf.NEXT_UP", [episode | {"Id": "e1"}])  # a show being watched
    new, failed = tool(setup(tmp_path), "whats_new")
    assert not failed and "my_list" not in new  # no providers: no My list from them
    assert [(e["jellyfin_id"], e["show"], e["episode"]) for e in new["library"]] == [
        ("e3", "Koffee with Karan", "S09E03")
    ]
    assert [e["episode"] for e in tool(setup(tmp_path), "whats_new", days=10)[0]["library"]] == ["S09E03", "S09E02"]


def test_whats_on_tv_says_what_jellyfin_is_playing(tmp_path, monkeypatch):
    jf_front = (
        "  mWakefulness=Awake\n    mResumedActivity: ActivityRecord{1 u0 org.jellyfin.androidtv/.ui.MainActivity t9}\n"
    )
    monkeypatch.setattr(Tv, "shell", lambda self, command: jf_front)
    episode = {
        "Id": "e8",
        "Name": "Episode 8",
        "SeriesName": "The Traitors (Telugu)",
        "ParentIndexNumber": 1,
        "IndexNumber": 8,
        "RunTimeTicks": 42_000_000_000,
    }
    playing = dict(TV_APP) | {
        "NowPlayingItem": episode,
        "PlayState": {"IsPaused": True, "PositionTicks": 12_600_000_000},
    }
    monkeypatch.setattr("tests.test_jf.SESSIONS", [playing])
    now, failed = tool(setup(tmp_path, tv_now=Tv("tv", tmp_path / "adbkey").now), "whats_on_tv")
    assert not failed and now["app_name"] == "Jellyfin"
    assert now["playing"] == {
        "title": "Episode 8",
        "show": "The Traitors (Telugu)",
        "episode": "S01E08",
        "paused": True,
        "minute": 21,
        "of_minutes": 70,
        "jellyfin_id": "e8",
    }
    monkeypatch.setattr("tests.test_jf.SESSIONS", [dict(TV_APP)])  # the app open, nothing playing
    assert (
        tool(setup(tmp_path, tv_now=Tv("tv", tmp_path / "adbkey").now), "whats_on_tv")[0]["playing"]
        == "nothing (browsing)"
    )


def test_a_provider_is_only_ever_one_set_up_here(tmp_path):
    # The provider's id goes into a path: anything else (from a model that was talked into it) could reach another route.
    c = setup(tmp_path, providers=[{"id": "p1", "name": "My IPTV"}])
    for bad in ("../settings/restart?", "p1/../../settings/providers/p1?", "p2"):
        text, failed = tool(c, "add_to_my_list", provider=bad, id="node1", title="A show")
        assert failed and "no such provider" in text, bad
