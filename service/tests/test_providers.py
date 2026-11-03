import json
import sqlite3
import subprocess

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimir_flix.jellyfin import Jellyfin
from mimir_flix.providers import Providers, router
from mimir_flix.vault import Vault, VaultError
from mimir_flix.wizard import allowed

PLAYLIST = """#EXTM3U url-tvg="http://tv.example/xmltv.php?username=me&password=s3cret"
#EXTINF:-1 tvg-id="star.in" group-title="India",Star Maa
http://tv.example/live/me/s3cret/101.ts
#EXTINF:-1 tvg-id="gemini.in",Gemini TV
http://tv.example/live/me/s3cret/102.ts
"""


def test_the_vault_keeps_logins_unreadable_without_its_key(tmp_path):
    db = tmp_path / "m.db"
    v = Vault(db, lambda: b"k" * 32)
    v.put("provider:abc", {"user": "me", "password": "s3cret"})
    assert v.get("provider:abc") == {"user": "me", "password": "s3cret"} and v.has("provider:abc")
    assert b"s3cret" not in db.read_bytes()  # the database alone holds nothing readable
    with pytest.raises(VaultError):
        Vault(db, lambda: b"x" * 32).get("provider:abc")  # another key
    raw = sqlite3.connect(db)
    raw.execute("UPDATE vault SET name = 'kodi' WHERE name = 'provider:abc'")  # a value moved to another name
    raw.commit()
    with pytest.raises(VaultError):
        v.get("kodi")
    assert v.delete("kodi") and v.get("kodi") is None


def test_a_vault_with_secrets_makes_no_new_key_when_the_keychain_cannot_be_read(tmp_path, monkeypatch):
    def security(args, **kw):  # the Keychain CLI over SSH: the login Keychain is locked, nothing found or added
        return subprocess.CompletedProcess(args, 36, "", "User interaction is not allowed.")

    monkeypatch.setattr("mimir_flix.vault.subprocess.run", security)
    config, db = tmp_path / "config", tmp_path / "m.db"
    Vault(db, lambda: b"k" * 32).put("kodi", {"password": "pw"})  # made in the login session, with the Keychain's key
    with pytest.raises(VaultError, match="Keychain"):
        Vault.for_mac(db, config).put("provider:x", {"user": "me", "password": "s3cret"})
    assert not (config / "vault-key").exists()  # a second key would split the vault
    fresh = Vault.for_mac(tmp_path / "new.db", config)  # an empty vault (Linux, a new Mac) makes its key file
    fresh.put("kodi", {"password": "pw"})
    assert fresh.get("kodi") == {"password": "pw"} and (config / "vault-key").exists()


class FakeJellyfin:
    def __init__(self):
        self.tuners, self.listings, self.refreshed = {}, {}, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p, body = request.url.path, json.loads(request.content or b"null")
        if p == "/LiveTv/TunerHosts" and request.method == "POST":
            self.tuners["t1"] = body
            return httpx.Response(200, json={"Id": "t1"})
        if p == "/LiveTv/TunerHosts" and request.method == "DELETE":
            self.tuners.pop(request.url.params["id"])
            return httpx.Response(204)
        if p == "/LiveTv/ListingProviders" and request.method == "POST":
            self.listings["l1"] = body
            return httpx.Response(200, json={"Id": "l1"})
        if p == "/LiveTv/ListingProviders" and request.method == "DELETE":
            self.listings.pop(request.url.params["id"])
            return httpx.Response(204)
        if p == "/ScheduledTasks":
            return httpx.Response(200, json=[{"Key": "RefreshGuide", "Id": "g"}])
        if p == "/ScheduledTasks/Running/g":
            self.refreshed += 1
            return httpx.Response(204)
        return httpx.Response(404)


def provider_side(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    if request.url.path == "/get.php":
        if (q.get("username"), q.get("password")) != ("me", "s3cret"):
            return httpx.Response(401)
        return httpx.Response(200, text=PLAYLIST)
    if request.url.path == "/xmltv.php":
        return httpx.Response(200, text="<tv><channel id='star.in'/></tv>")
    return httpx.Response(404)


def setup(tmp_path):
    config = tmp_path / "config"
    vault = Vault(tmp_path / "m.db", lambda: b"k" * 32)
    fake = FakeJellyfin()
    jf = Jellyfin("http://jf", "key", transport=httpx.MockTransport(fake))
    providers = Providers(config, vault, lambda: jf, 8723, transport=httpx.MockTransport(provider_side))
    app = FastAPI()
    app.include_router(router(providers, allowed))
    here = TestClient(app, client=("127.0.0.1", 5000))  # Jellyfin, or the Settings page, on this Mac
    return here, fake, vault, config


def test_an_xtream_provider_plays_through_jellyfin_and_its_login_stays_in_mimir_flix(tmp_path):
    c, fake, vault, config = setup(tmp_path)
    xtream = {"name": "Home IPTV", "kind": "xtream", "server": "http://tv.example", "user": "me", "password": "nope"}
    assert c.post("/api/settings/providers", json=xtream).json()["detail"] == "the provider refused the login"
    assert fake.tuners == {} and c.get("/api/settings/providers").json() == []  # nothing left behind
    added = c.post("/api/settings/providers", json=xtream | {"password": "s3cret"}).json()
    pid = added["id"]
    assert added["channels"] == 2 and fake.refreshed == 1

    # Jellyfin is given mimir-flix's own addresses, never the provider's.
    assert fake.tuners["t1"]["Url"] == f"http://127.0.0.1:8723/iptv/{pid}/playlist.m3u"
    assert fake.listings["l1"]["Path"] == f"http://127.0.0.1:8723/iptv/{pid}/guide.xml"
    ours = c.get(f"/iptv/{pid}/playlist.m3u").text
    assert "s3cret" not in ours and "url-tvg" not in ours and ours.startswith("#EXTM3U")
    assert f"http://127.0.0.1:8723/iptv/{pid}/stream/1" in ours and "Gemini TV" in ours
    hop = c.get(f"/iptv/{pid}/stream/1", follow_redirects=False)
    assert hop.status_code == 302 and hop.headers["location"] == "http://tv.example/live/me/s3cret/102.ts"
    assert "star.in" in c.get(f"/iptv/{pid}/guide.xml").text

    # The login is in the vault, not in settings.json, and nobody but this Mac reads the links.
    assert "s3cret" not in (config / "settings.json").read_text()
    assert vault.get(f"provider:{pid}") == {"user": "me", "password": "s3cret"}
    phone = TestClient(c.app, client=("127.0.0.1", 5000), headers={"X-Forwarded-For": "100.64.0.2"})
    assert phone.get(f"/iptv/{pid}/stream/1", follow_redirects=False).status_code == 403
    assert "s3cret" not in json.dumps(phone.get("/api/settings/providers").json())
    lan = TestClient(c.app, client=("192.0.2.30", 5000))
    assert lan.post("/api/settings/providers", json=xtream).status_code == 403

    # Removing it takes it out of Jellyfin and forgets the login.
    assert phone.delete(f"/api/settings/providers/{pid}").status_code == 204
    assert fake.tuners == {} and fake.listings == {} and vault.get(f"provider:{pid}") is None
    assert c.get(f"/iptv/{pid}/playlist.m3u").status_code == 422


def test_a_playlist_that_is_not_m3u_is_refused(tmp_path):
    c, fake, _, _ = setup(tmp_path)
    bad = {"name": "x", "kind": "m3u", "url": "http://tv.example/get.php?username=me&password=s3cret&x=1"}
    assert c.post("/api/settings/providers", json=bad).status_code == 201  # a real playlist behind that address
    page = {"name": "y", "kind": "m3u", "url": "http://tv.example/xmltv.php"}
    assert c.post("/api/settings/providers", json=page).json()["detail"] == "that is not an M3U playlist"
    assert c.post("/api/settings/providers", json={"name": "z", "kind": "m3u", "url": "ftp://x"}).status_code == 422
