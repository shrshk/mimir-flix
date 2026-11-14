"""Wholphin's home page (wholphin.py) against Jellyfin at its HTTP edge: titles with the tags mirror.py writes, a
studio, and the user's display preferences."""

import json

import httpx

from mimir_tv import wholphin
from mimir_tv.jellyfin import Jellyfin

SHOWS, MOVIES = "lib-shows", "lib-movies"
TITLES = [  # (library, type, tags)
    (SHOWS, "Series", ["Telugu", "TV serial", "Telugu · TV serial"]),
    (SHOWS, "Series", ["Hindi", "Web series", "Hindi · Web series"]),
    (SHOWS, "Series", ["English"]),
    (MOVIES, "Movie", ["Telugu", "Indian movies", "Telugu · Indian movies"]),
    (MOVIES, "Movie", ["English", "4K", "English · 4K"]),
    ("lib-apps", "Movie", ["Netflix", "Movie"]),
]


class FakeJellyfin:
    def __init__(self):
        self.prefs = {"Id": "default", "CustomPrefs": {"other": "kept"}, "SortBy": "SortName"}
        self.posts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        q, path = request.url.params, request.url.path
        if path == "/Users":
            return httpx.Response(200, json=[{"Id": "u1", "Policy": {"IsAdministrator": True}}])
        if path == "/Library/VirtualFolders":
            return httpx.Response(
                200,
                json=[
                    {"Name": "My TV", "ItemId": SHOWS},
                    {"Name": "My TV Movies", "ItemId": MOVIES},
                    {"Name": "On my apps", "ItemId": "lib-apps"},
                ],
            )
        if path == "/Items" and q.get("IncludeItemTypes") == "Playlist":
            lists = [
                {"Name": "Popular on Netflix", "Id": "pl-netflix"},
                {"Name": "Trending on your apps", "Id": "pl-trend"},
            ]
            return httpx.Response(200, json={"Items": lists})
        if path == "/Items":
            n = sum(
                1
                for lib, kind, tags in TITLES
                if lib == q["ParentId"] and kind == q["IncludeItemTypes"] and q["Tags"] in tags
            )
            return httpx.Response(200, json={"Items": [], "TotalRecordCount": n})
        if path == "/Studios":
            hits = [{"Name": "Channel One", "Id": "st1"}] if "one" in q["SearchTerm"].lower() else []
            return httpx.Response(200, json={"Items": hits})
        if path == "/DisplayPreferences/default":
            assert q["client"] == "Wholphin" and q["userId"] == "u1"
            if request.method == "POST":
                self.prefs = json.loads(request.content)
                self.posts += 1
                return httpx.Response(204)
            return httpx.Response(200, json=self.prefs)
        return httpx.Response(404)


def test_wholphins_home_is_laid_out_by_language_and_kind_in_the_users_order():
    fake = FakeJellyfin()
    jf = Jellyfin("http://jf", "k", transport=httpx.MockTransport(fake.handler))
    cfg = {
        "wholphin_home": {"languages": ["Telugu", "Hindi", "English"], "channels": ["channel one", "Nowhere"]},
        "streaming_apps": "netflix,peacock",  # Peacock has nothing in On my apps: no row
    }
    assert wholphin.home(jf, "My TV", "My TV Movies", cfg) == "9 rows written"

    saved = json.loads(fake.prefs["CustomPrefs"]["home_settings"])
    assert saved["version"] == 1 and fake.prefs["CustomPrefs"]["other"] == "kept" and fake.prefs["SortBy"] == "SortName"
    names = [
        r.get("name") or r.get("parentId") if r["type"] == "ByParent" else r.get("name") or r["type"]
        for r in saved["rows"]
    ]
    # Continue watching first, then the apps, then languages in the order asked; a kind with nothing in it is left out; English shows have no kind, so one row;
    # an unknown channel is left out; new episodes last.
    assert names == [
        "ContinueWatchingCombined",
        "pl-trend",
        "pl-netflix",
        "Telugu TV serials",
        "Telugu movies",
        "Hindi web series",
        "English shows",
        "English movies",
        "Channel One",
    ]
    serials = saved["rows"][3]["filter"]
    assert serials["tags"] == ["Telugu · TV serial"] and serials["parentId"] == SHOWS
    assert serials["includeItemTypes"] == ["Series"] and serials["sortBy"] == ["DateCreated"]
    # The apps' rows are mimir-tv's own lists: trending first, then each app (Peacock has none: no row).
    assert saved["rows"][1] == {"type": "ByParent", "parentId": "pl-trend", "recursive": False}
    assert saved["rows"][8]["filter"]["studioIds"] == ["st1"]

    # The same layout again is not written again.
    assert wholphin.home(jf, "My TV", "My TV Movies", cfg).endswith("unchanged") and fake.posts == 1
    assert wholphin.home(jf, "My TV", "My TV Movies", {}) == "no wholphin_home languages in Settings"
