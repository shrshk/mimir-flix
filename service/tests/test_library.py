import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mimir_flix.app import create_app
from mimir_flix.jellyfin import Jellyfin


class FakeJellyfin:
    """Jellyfin at its HTTP edge: virtual folders kept in memory, scans recorded."""

    def __init__(self):
        self.libs: dict[str, dict] = {}
        self.scans: list[str] = []
        self.running = None
        self.played: list[str] = []
        self.removed: set[str] = set()  # items deleted in Jellyfin (their files may still be there)
        self.favourites: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        q, path, m = request.url.params, request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/Library/VirtualFolders" and m == "GET":
            return httpx.Response(200, json=list(self.libs.values()))
        if path == "/Library/VirtualFolders" and m == "POST":
            paths = [p["Path"] for p in body["LibraryOptions"]["PathInfos"]]
            self.libs[q["name"]] = {
                "Name": q["name"],
                "CollectionType": q["collectionType"],
                "Locations": paths,
                "ItemId": f"id-{q['name']}",
                "RefreshStatus": "Idle",
            }
            return httpx.Response(204)
        if path == "/Library/VirtualFolders" and m == "DELETE":
            self.libs.pop(q["name"])
            return httpx.Response(204)
        if path == "/Library/VirtualFolders/Name":
            lib = self.libs.pop(q["name"])
            lib["Name"] = q["newName"]
            self.libs[q["newName"]] = lib
            return httpx.Response(204)
        if path == "/Library/VirtualFolders/Paths" and m == "POST":
            self.libs[body["Name"]]["Locations"].append(body["PathInfo"]["Path"])
            return httpx.Response(204)
        if path == "/Library/VirtualFolders/Paths" and m == "DELETE":
            self.libs[q["name"]]["Locations"].remove(q["path"])
            return httpx.Response(204)
        if path.startswith("/Items/") and path.endswith("/Refresh"):
            self.scans.append(path.split("/")[2])
            return httpx.Response(204)
        if path.startswith("/UserFavoriteItems/"):
            item = path.split("/")[2]
            self.favourites.add(item) if m == "POST" else self.favourites.discard(item)
            return httpx.Response(200, json={"IsFavorite": m == "POST"})
        if path == "/Library/Refresh":
            self.scans.append("all")
            return httpx.Response(204)
        if path == "/ScheduledTasks":
            tasks = [{"Key": "RefreshLibrary", "Name": "Scan Media Library", "State": "Idle"}]
            if self.running is not None:
                tasks[0] |= {"State": "Running", "CurrentProgressPercentage": self.running}
            return httpx.Response(200, json=tasks)
        if path == "/Items" and q.get("Filters") == "IsFavorite" and q.get("Fields") == "Path":  # favourited shows
            shows = [d for lib in self.libs.values() if lib["CollectionType"] == "tvshows" for r in lib["Locations"]
                     for d in Path(r).iterdir() if d.is_dir()]
            hits = [{"Id": f"series:{d.name}", "Path": str(d)} for d in shows if f"series:{d.name}" in self.favourites]
            return httpx.Response(200, json={"Items": hits})
        if path == "/Items" and q.get("Fields") == "Path":  # what a scan found: .strm files, and each show's folder
            lib = next(x for x in self.libs.values() if x["ItemId"] == q["ParentId"])
            items = []
            for root in map(Path, lib["Locations"]):
                if lib["CollectionType"] == "tvshows":
                    items += [
                        {"Id": f"series:{d.name}", "Type": "Series", "Path": str(d)}
                        for d in root.iterdir()
                        if d.is_dir()
                    ]
                items += [{"Id": f"strm:{f.stem}", "Path": str(f)} for f in root.rglob("*.strm")]
            return httpx.Response(200, json={"Items": [i for i in items if i["Id"] not in self.removed]})
        if path == "/Items":
            return httpx.Response(200, json={"TotalRecordCount": 7})
        if path == "/Users":
            return httpx.Response(200, json=[{"Id": "u1", "Policy": {"IsAdministrator": True}}])
        if path == "/Sessions":
            return httpx.Response(
                200,
                json=[
                    {
                        "Id": "tv",
                        "Client": "Jellyfin Android TV",
                        "SupportsRemoteControl": True,
                        "PlayableMediaTypes": ["Video"],
                    }
                ],
            )
        if path == "/Sessions/tv/Playing":
            self.played.append(q["itemIds"])
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture
def jf():
    return FakeJellyfin()


@pytest.fixture
def c(jf, tmp_path):
    client = Jellyfin("http://jf", "key", transport=httpx.MockTransport(jf.handler))
    return TestClient(create_app(client, tmp_path / "picks"), client=("127.0.0.1", 5000))  # Settings, on this Mac


@pytest.fixture
def drive(tmp_path):
    root = tmp_path / "drive"
    for d in ("wedding/film", "wedding/Raw", "movies"):
        (root / d).mkdir(parents=True)
    (root / "wedding/film/film.mp4").write_bytes(b"f")
    (root / "movies/one.mkv").write_bytes(b"m")
    return root


def test_library_and_folder_crud(c, jf, drive):
    assert (
        c.post("/api/libraries", json={"name": "Wedding", "folders": [str(drive / "wedding/film")]}).status_code == 201
    )
    assert c.post("/api/libraries", json={"name": "Wedding"}).status_code == 409

    # A parent folder replaces the child folders it covers; a child of an included folder is refused.
    assert c.post("/api/libraries/Wedding/folders", json={"path": str(drive / "wedding")}).status_code == 201
    assert jf.libs["Wedding"]["Locations"] == [str(drive / "wedding")]
    assert c.post("/api/libraries/Wedding/folders", json={"path": str(drive / "wedding/film")}).status_code == 409

    assert c.put("/api/libraries/Wedding", json={"name": "Our wedding"}).status_code == 200
    lib = c.get("/api/libraries").json()[0]
    assert (lib["name"], lib["kind"], lib["items"]) == ("Our wedding", "homevideos", 7)

    assert c.delete("/api/libraries/Our wedding/folders", params={"path": str(drive / "wedding")}).status_code == 204
    assert jf.libs["Our wedding"]["Locations"] == []
    assert c.delete("/api/libraries/Our wedding").status_code == 204
    assert jf.libs == {}
    assert (drive / "wedding/film/film.mp4").exists()  # files are never touched


def test_exclude_a_subfolder_and_include_it_again(c, drive):
    c.post("/api/libraries", json={"name": "Wedding", "folders": [str(drive / "wedding")]})
    assert c.post("/api/libraries/Wedding/exclude", json={"path": str(drive / "wedding/Raw")}).status_code == 201
    assert (drive / "wedding/Raw/.ignore").is_file()
    assert c.get("/api/libraries").json()[0]["folders"][0]["excluded"] == [str(drive / "wedding/Raw")]
    # Only inside the library, and never the library folder itself.
    assert c.post("/api/libraries/Wedding/exclude", json={"path": str(drive / "movies")}).status_code == 422
    assert c.post("/api/libraries/Wedding/exclude", json={"path": str(drive / "wedding")}).status_code == 422

    assert (
        c.request("DELETE", "/api/libraries/Wedding/exclude", params={"path": str(drive / "wedding/Raw")}).status_code
        == 204
    )
    assert not (drive / "wedding/Raw/.ignore").exists()


def test_single_videos_go_through_the_picks_folder(c, jf, drive, tmp_path):
    c.post("/api/libraries", json={"name": "Movies", "kind": "movies"})
    film = drive / "movies/one.mkv"
    assert c.post("/api/libraries/Movies/videos", json={"path": str(film)}).status_code == 201
    picks = tmp_path / "picks/Movies"
    assert (picks / "one.mkv").resolve() == film.resolve()
    assert jf.libs["Movies"]["Locations"] == [str(picks)]
    assert c.post("/api/libraries/Movies/videos", json={"path": str(film)}).status_code == 409
    assert c.post("/api/libraries/Movies/videos", json={"path": str(drive / "wedding")}).status_code == 422

    lib = c.get("/api/libraries").json()[0]
    assert lib["folders"] == [] and [v["name"] for v in lib["videos"]] == ["one.mkv"]

    assert c.delete("/api/libraries/Movies/videos/one.mkv").status_code == 204
    assert not (picks / "one.mkv").exists() and film.exists()
    assert jf.scans == ["id-Movies"]


def test_live_scan_status_and_browse(c, jf, drive):
    assert c.get("/api/status").json() == {"scan": None}
    jf.running = 42.456
    assert c.get("/api/status").json() == {"scan": {"progress": 42.5}}
    assert c.post("/api/scan").status_code == 202 and jf.scans == ["all"]

    b = c.get("/api/browse", params={"path": str(drive / "movies")}).json()
    assert b["dirs"] == [] and b["videos"] == [{"name": "one.mkv", "bytes": 1}]
    assert Path(b["parent"]) == drive


def test_a_device_on_the_wifi_cannot_look_through_the_mac_or_change_libraries(c, jf, drive):
    wifi = TestClient(c.app, client=("192.0.2.30", 5000))
    assert wifi.get("/api/browse", params={"path": str(drive)}).status_code == 403
    assert wifi.post("/api/libraries", json={"name": "Mine", "folders": [str(drive)]}).status_code == 403
    assert wifi.post("/api/libraries/Movies/videos", json={"path": str(drive / "movies" / "one.mkv")}).status_code == 403
    assert wifi.post("/api/scan").status_code == 403 and jf.scans == []
    assert wifi.get("/api/libraries").status_code == 200  # browsing what is shared stays open


def test_only_its_own_names_and_its_own_pages_reach_it(jf, tmp_path):
    client = Jellyfin("http://jf", "key", transport=httpx.MockTransport(jf.handler))
    app = create_app(client, tmp_path / "picks", hosts=["flix.example.com"])
    here = TestClient(app, client=("127.0.0.1", 5000))
    for host in ("flix.example.com", "127.0.0.1:8723", "[::1]:8723", "localhost:8723", "mac.local", "mac.tail1.ts.net"):
        assert here.get("/api/libraries", headers={"Host": host}).status_code == 200, host
    # A web page that points its own name at this Mac (DNS rebinding) gets nothing.
    assert here.get("/api/libraries", headers={"Host": "evil.example.net"}).status_code == 421
    # Another site's page may not change anything, even from this Mac; this app's own pages and tools may.
    other = {"Host": "flix.example.com", "Origin": "https://evil.example.net"}
    assert here.post("/api/scan", headers=other).status_code == 403 and jf.scans == []
    assert here.post("/api/scan", headers={"Host": "flix.example.com", "Origin": "null"}).status_code == 403
    assert here.post("/api/scan", headers={"Host": "flix.example.com", "Origin": "https://flix.example.com"}).status_code == 202
    assert here.post("/api/scan", headers={"Host": "127.0.0.1:8723"}).status_code == 202  # no Origin: not a browser
    page = here.get("/api/libraries", headers={"Host": "flix.example.com"})
    assert page.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_the_page_version_changes_with_the_page(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("v1")
    monkeypatch.setattr("mimir_flix.app.STATIC", static)
    c = TestClient(
        create_app(Jellyfin("http://jf", "k", transport=httpx.MockTransport(FakeJellyfin().handler)), tmp_path)
    )
    first = c.get("/api/version").json()["page"]
    assert c.get("/").headers["cache-control"] == "no-cache"
    (static / "index.html").write_text("v2")
    assert c.get("/api/version").json()["page"] != first


def test_the_page_is_told_which_connectors_are_set_up(tmp_path):
    jf = Jellyfin("http://jf", "k", transport=httpx.MockTransport(FakeJellyfin().handler))
    c = TestClient(create_app(jf, tmp_path, connectors={"providers": lambda: [], "kodi": False, "tv": True}))
    assert c.get("/api/connectors").json() == {"providers": [], "kodi": False, "tv": True}


def test_the_tv_app_is_served_for_the_tvs_downloader_once_it_is_built(c, tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_FLIX_DATA", str(tmp_path / "data"))
    missing = c.get("/tv/app.apk")
    assert missing.status_code == 404 and "mimir-flix-tv" in missing.json()["detail"]
    (tmp_path / "data" / "tv").mkdir(parents=True)
    (tmp_path / "data" / "tv" / "mimir-flix-tv.apk").write_bytes(b"PK\x03\x04apk")
    got = c.get("/tv/app.apk")
    assert got.content == b"PK\x03\x04apk"
    assert got.headers["content-type"] == "application/vnd.android.package-archive"
