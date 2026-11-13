"""On my apps (onapps.py): the streaming apps' popular titles as Jellyfin titles that launch their app."""

import json
from urllib.parse import parse_qs, urlsplit

import httpx

from mimir_flix import onapps
from mimir_flix.jellyfin import Jellyfin


class FakeStreaming:
    ready = True

    def __init__(self, items):
        self.items = items

    def catalog(self, per_app):
        return self.items


def item(title, tmdb, apps, kind="movie", year="2025"):
    return {
        "title": title,
        "year": year,
        "kind": kind,
        "tmdb": tmdb,
        "thumb": f"https://image.example/{tmdb.replace('/', '-')}.jpg",
        "desc": f"About {title}",
        "apps": [{"app": a.lower(), "name": a} for a in apps],
    }


def test_the_apps_popular_titles_become_jellyfin_titles_that_launch_their_app(tmp_path):
    root = tmp_path / "apps"
    poster = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"jpg", headers={"content-type": "image/jpeg"})
        )
    )
    s = FakeStreaming(
        [item("Lupin", "tv/96677", ["Netflix"], "show"), item("Heat", "movie/949", ["Netflix", "Peacock"])]
    )
    done = onapps.fill(s, root, "https://lan.example", poster, now=1000)
    assert done == {"titles": 2, "new": 2, "removed": 0, "posters": 2}
    line = (root / "Lupin (2025)/Lupin (2025).strm").read_text().strip()
    u = urlsplit(line)
    assert u.path == "/api/streaming/launch" and parse_qs(u.query) == {
        "app": ["netflix"],
        "tmdb": ["tv/96677"],
        "title": ["Lupin"],
    }
    nfo = (root / "Heat (2025)/movie.nfo").read_text()
    assert "<genre>Netflix</genre>" in nfo and "<genre>Peacock</genre>" in nfo and "<genre>Movie</genre>" in nfo
    assert "<tag>Netflix</tag>" in nfo and "<plot>About Heat</plot>" in nfo
    assert "<genre>Show</genre>" in (root / "Lupin (2025)/movie.nfo").read_text()
    assert (root / "Heat (2025)/poster.jpg").read_bytes() == b"jpg"

    # Still listed: kept, not new. Gone from the list for two weeks: its files are removed.
    s.items = s.items[:1]
    assert onapps.fill(s, root, "https://lan.example", poster, now=2000)["new"] == 0
    assert (root / "Heat (2025)").exists()
    later = onapps.fill(s, root, "https://lan.example", poster, now=2000 + onapps.KEEP_FOR + 1)
    assert later["removed"] == 1 and not (root / "Heat (2025)").exists() and (root / "Lupin (2025)").exists()


class FakeJellyfin:
    """Jellyfin's edge for playlists: the On my apps library's titles, and playlists that keep their order."""

    def __init__(self, root):
        self.root, self.lists, self.writes = root, {}, 0

    def handler(self, request):
        q, path, m = request.url.params, request.url.path, request.method
        if path == "/Users":
            return httpx.Response(200, json=[{"Id": "u1", "Policy": {"IsAdministrator": True}}])
        if path == "/Library/VirtualFolders":
            return httpx.Response(200, json=[{"Name": "On my apps", "ItemId": "lib"}])
        if path == "/Items" and q.get("IncludeItemTypes") == "Playlist":
            return httpx.Response(200, json={"Items": [{"Name": n, "Id": n} for n in self.lists]})
        if path == "/Items":
            items = [
                {"Id": f"id-{d.name}", "Path": str(d / f"{d.name}.strm")} for d in self.root.iterdir() if d.is_dir()
            ]
            return httpx.Response(200, json={"Items": items})
        if path == "/Playlists" and m == "POST":
            body = json.loads(request.content)
            self.lists[body["Name"]] = list(body["Ids"])
            self.writes += 1
            return httpx.Response(200, json={"Id": body["Name"]})
        if path.startswith("/Playlists/") and path.endswith("/Items"):
            name = path.split("/")[2]
            if m == "GET":
                rows = [{"Id": i, "PlaylistItemId": f"e{n}"} for n, i in enumerate(self.lists[name])]
                return httpx.Response(200, json={"Items": rows})
            if m == "DELETE":
                self.lists[name] = []
            else:
                self.lists[name] += q["Ids"].split(",")
            self.writes += 1
            return httpx.Response(204)
        return httpx.Response(404)


def test_mimir_flixs_app_rows_become_playlists_with_the_same_titles_in_the_same_order(tmp_path):
    root = tmp_path / "apps"
    lupin, heat, up = (
        item("Lupin", "tv/1", ["Netflix"], "show"),
        item("Heat", "movie/2", ["Netflix"]),
        item("Up", "movie/3", ["Peacock"]),
    )
    s = FakeStreaming([lupin, heat, up])
    s.trending = lambda: [up, lupin]
    s.rows = lambda per_row, at_least: [{"name": "Netflix", "items": [heat, lupin]}, {"name": "Peacock", "items": [up]}]
    no_posters = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    onapps.fill(s, root, "https://lan.example", no_posters)
    fake = FakeJellyfin(root)
    jf = Jellyfin("http://jf", "k", transport=httpx.MockTransport(fake.handler))

    assert onapps.playlists(s, jf, root) == 3
    assert fake.lists == {
        "Trending on your apps": ["id-Up (2025)", "id-Lupin (2025)"],
        "Popular on Netflix": ["id-Heat (2025)", "id-Lupin (2025)"],  # mimir-flix's order, not Jellyfin's
        "Popular on Peacock": ["id-Up (2025)"],
    }
    writes = fake.writes
    assert onapps.playlists(s, jf, root) == 0 and fake.writes == writes  # unchanged: left alone
    s.rows = lambda per_row, at_least: [{"name": "Netflix", "items": [lupin, heat]}]  # the order changed
    assert onapps.playlists(s, jf, root) == 1
    assert fake.lists["Popular on Netflix"] == ["id-Lupin (2025)", "id-Heat (2025)"]
