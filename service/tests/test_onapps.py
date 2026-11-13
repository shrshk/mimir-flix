"""On my apps (onapps.py): the streaming apps' popular titles as Jellyfin titles that launch their app."""

from urllib.parse import parse_qs, urlsplit

import httpx

from mimir_flix import onapps


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
