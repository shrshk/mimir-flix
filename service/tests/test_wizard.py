import json

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimir_tv import wizard
from mimir_tv.vault import Vault

KEY = "jf-key-123"


class FakeJellyfin:
    """A new Jellyfin: its own first run not done, one admin once created, libraries added by the test."""

    def __init__(self):
        self.admin: tuple[str, str] | None = None
        self.libraries: list[dict] = []
        self.encoding = {"HardwareAccelerationType": "none"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, body = request.url.path, json.loads(request.content or b"null")
        if path == "/System/Info/Public":
            return httpx.Response(200, json={"StartupWizardCompleted": self.admin is not None})
        if path.startswith("/Startup/"):
            if path == "/Startup/User" and request.method == "POST":
                self.admin = (body["Name"], body["Password"])
            return httpx.Response(204) if request.method == "POST" else httpx.Response(200, json={})
        if path == "/Users/AuthenticateByName":
            ok = self.admin == (body["Username"], body["Pw"])
            return httpx.Response(200, json={"AccessToken": "session"}) if ok else httpx.Response(401)
        if path == "/Auth/Keys":
            return (
                httpx.Response(204)
                if request.method == "POST"
                else httpx.Response(200, json={"Items": [{"AppName": "mimir-tv", "AccessToken": KEY}]})
            )
        if path == "/System/Configuration/encoding":
            if request.method == "POST":
                self.encoding = body
                return httpx.Response(204)
            return httpx.Response(200, json=self.encoding)
        if path == "/Library/VirtualFolders":
            if KEY not in request.headers.get("authorization", ""):
                return httpx.Response(401)
            return httpx.Response(200, json=self.libraries)
        return httpx.Response(404)


def setup(tmp_path):
    jf, restarts = FakeJellyfin(), []
    vault = Vault(tmp_path / "m.db", lambda: b"k" * 32)  # a fixed key for the test (the Keychain's on the Mac)
    app = FastAPI()
    app.include_router(
        wizard.router(
            tmp_path / "config",
            tmp_path / "m.db",
            vault,
            httpx.MockTransport(jf),
            restart=lambda: restarts.append(1),
        )
    )
    return TestClient(app, client=("127.0.0.1", 5000)), jf, vault, restarts  # the page opened on the Mac itself


def states(c) -> dict:
    return {s["key"]: s["state"] for s in c.get("/api/setup").json()["steps"]}


def test_a_new_mac_goes_through_every_step_and_the_wizard_locks_at_finish(tmp_path, monkeypatch):
    monkeypatch.delenv("MIMIR_TV_HOST", raising=False)
    c, jf, vault, restarts = setup(tmp_path)
    config = tmp_path / "config"
    assert set(states(c).values()) == {"todo"} and c.get("/api/setup").json()["locked"] is False

    # Jellyfin: its own first run makes this the admin; the key is kept owner-only; hardware decoding on.
    step = {"url": "http://jf:8096/", "name": "admin", "password": "pw-secret"}
    assert c.post("/api/setup/jellyfin", json=step).status_code == 200
    key_file = config / "jellyfin-api-key"
    assert key_file.read_text().strip() == KEY and key_file.stat().st_mode & 0o777 == 0o600
    assert jf.encoding["HardwareAccelerationType"] == "videotoolbox" and restarts == [1]
    assert states(c)["jellyfin"] == "done"

    # Media is done only once a library exists.
    assert c.post("/api/setup/media").status_code == 409
    jf.libraries = [{"Name": "Movies", "Locations": ["/Volumes/d/Movies"]}]
    assert states(c)["media"] == "done"

    assert c.post("/api/setup/streaming", json={"apps": "hbo"}).status_code == 422  # no token yet
    assert c.post("/api/setup/streaming", json={"token": "tmdb-secret", "apps": "nope"}).status_code == 422
    assert (
        c.post("/api/setup/streaming", json={"token": "tmdb-secret", "apps": "netflix", "region": "in"}).status_code
        == 200
    )
    assert (
        c.post(
            "/api/setup/tv",
            json={"host": "192.0.2.25", "kodi_url": "http://192.0.2.25:8080", "kodi_password": "kodi-secret"},
        ).status_code
        == 200
    )
    assert c.post("/api/setup/finish").status_code == 409  # phones still to do
    assert c.post("/api/setup/phones", json={"direct": True, "resolver": "ftp://x"}).status_code == 422
    assert c.post("/api/setup/phones/skip").status_code == 200
    assert c.post("/api/setup/finish").status_code == 409  # backups still to do
    assert c.post("/api/setup/backup", json={"dir": str(tmp_path / "unplugged")}).status_code == 422
    (tmp_path / "backups").mkdir()
    assert c.post("/api/setup/backup", json={"dir": str(tmp_path / "backups")}).status_code == 200

    assert c.post("/api/setup/finish").status_code == 409  # languages still to do (saved or skipped)
    assert c.post("/api/setup/languages", json={"languages": " , "}).status_code == 422
    assert c.post("/api/setup/languages", json={"languages": "Telugu, Hindi, Telugu, English"}).status_code == 200
    assert c.post("/api/setup/finish").json() == {"restarting": True} and restarts == [1, 1]
    status = c.get("/api/setup").json()
    assert status["locked"] is True and states(c)["phones"] == "skipped"
    # Locked: nothing changes any more from here.
    assert c.post("/api/setup/streaming", json={"token": "x", "apps": "netflix"}).status_code == 409
    assert c.post("/api/setup/finish").status_code == 409

    cfg = json.loads((config / "settings.json").read_text())
    assert cfg == {
        "jellyfin_url": "http://jf:8096",
        "streaming_apps": "netflix",
        "streaming_region": "IN",
        "tvs": [{"name": "TV", "host": "192.0.2.25", "kind": "android"}],
        "kodi_url": "http://192.0.2.25:8080",
        "kodi_password_file": "vault:kodi",
        "backup_dir": str(tmp_path / "backups"),
        "library_languages": ["Telugu", "Hindi", "English"],  # in order, once each
    }
    # No password is in the settings or the wizard's database.
    stored = (config / "settings.json").read_text() + (tmp_path / "m.db").read_bytes().decode("latin-1")
    assert not any(s in stored for s in ("pw-secret", "tmdb-secret", "kodi-secret"))


def test_a_wrong_jellyfin_password_saves_nothing_and_jellyfin_cannot_be_skipped(tmp_path):
    c, jf, _, restarts = setup(tmp_path)
    jf.admin = ("admin", "right")
    r = c.post("/api/setup/jellyfin", json={"url": "http://jf:8096", "name": "admin", "password": "wrong"})
    assert r.status_code == 401 and restarts == []
    assert not (tmp_path / "config/jellyfin-api-key").exists() and states(c)["jellyfin"] == "todo"
    assert c.post("/api/setup/jellyfin/skip").status_code == 409


def test_a_mac_already_set_up_is_locked_from_the_start(tmp_path):
    c, jf, _, _ = setup(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "jellyfin-api-key").write_text(KEY)
    (config / "tmdb-token").write_text("t")
    jf.libraries = [{"Name": "Movies", "Locations": []}]
    (config / "settings.json").write_text(
        json.dumps(
            {
                "jellyfin_url": "http://jf:8096",
                "tv_host": "tv",
                "resolver": "https://lan.example",
                "backup_dir": str(tmp_path),
            }
        )
    )
    assert c.get("/api/setup").json()["locked"] is True
    assert c.post("/api/setup/tv", json={"host": "evil"}).status_code == 409


def test_settings_change_after_setup_from_the_mac_or_the_tailnet_only_and_never_show_a_secret(tmp_path):
    c, jf, vault, restarts = setup(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "jellyfin-api-key").write_text(KEY)
    elsewhere = tmp_path / "kodi-pw"
    elsewhere.write_text("old")
    (config / "settings.json").write_text(json.dumps({"tv_host": "192.0.2.25", "kodi_password_file": str(elsewhere)}))
    lan = TestClient(c.app, client=("192.0.2.30", 5000))
    phone = TestClient(c.app, client=("127.0.0.1", 5000), headers={"X-Forwarded-For": "100.64.0.2"})  # via Caddy

    # What is set (addresses, folders, the off-site bucket) is for those who may change it, not any Wi-Fi device.
    assert lan.get("/api/settings").status_code == 403 and lan.get("/api/setup").status_code == 403
    assert phone.get("/api/settings").status_code == 200
    # The older one-TV setting reads as the first TV.
    assert c.get("/api/settings").json()["tvs"] == [{"name": "TV", "host": "192.0.2.25", "kind": "android"}]
    assert c.get("/api/settings").json()["kodi_password"] is True  # saved outside the config folder
    keep = {"tvs": [{"name": "TV", "host": "192.0.2.25"}], "kodi_url": "http://192.0.2.25:8080"}
    assert c.put("/api/settings/tvs", json=keep).status_code == 200
    assert json.loads((config / "settings.json").read_text())["kodi_password_file"] == str(elsewhere)
    two = {
        "tvs": [
            {"name": "Living room", "host": "192.0.2.25"},
            {"name": "Bedroom", "host": "192.0.2.40", "kind": "fire"},
        ],
        "kodi_password": "kodi-secret",
        "kodi_url": "http://192.0.2.25:8080",
    }
    assert lan.put("/api/settings/tvs", json=two).status_code == 403  # any device on the Wi-Fi: no
    assert phone.put("/api/settings/tvs", json=two).json() == {"restart_needed": True}
    # Saving the TVs again without a password keeps the one saved.
    assert phone.put("/api/settings/tvs", json=two | {"kodi_password": ""}).status_code == 200
    assert json.loads((config / "settings.json").read_text())["kodi_password_file"] == "vault:kodi"
    dup = {"tvs": [{"host": "192.0.2.25"}, {"host": "192.0.2.25"}]}
    assert phone.put("/api/settings/tvs", json=dup).status_code == 422
    assert phone.put("/api/settings/streaming", json={"token": "tmdb-secret", "apps": "netflix"}).status_code == 200
    # The iPhones' Jellyfin app: saved with the Phones card; one Settings does not know is refused.
    assert phone.put("/api/settings/phones", json={"direct": True, "phone_app": "streamyfin"}).status_code == 200
    assert json.loads((config / "settings.json").read_text())["phone_app"] == "streamyfin"
    assert phone.put("/api/settings/phones", json={"phone_app": "infuse"}).status_code == 422
    assert phone.get("/api/settings").json()["phone_app"] == "streamyfin"

    shown = c.get("/api/settings").json()
    assert [t["name"] for t in shown["tvs"]] == ["Living room", "Bedroom"] and shown["tv_host"] == "192.0.2.25"
    assert shown["kodi_password"] is True and shown["tmdb_token"] is True and shown["restart_needed"] is True
    assert not any(s in json.dumps(shown) for s in ("kodi-secret", "tmdb-secret", KEY))
    cfg = json.loads((config / "settings.json").read_text())
    assert "tv_host" not in cfg and cfg["tvs"][1] == {"name": "Bedroom", "host": "192.0.2.40", "kind": "fire"}
    assert vault.get("kodi") == {"password": "kodi-secret"}

    # A TV that was never saved cannot be paired.
    assert phone.post("/api/settings/tvs/pair", json={"host": "10.9.9.9"}).status_code == 404
    assert lan.post("/api/settings/restart").status_code == 403 and restarts == []
    assert phone.post("/api/settings/restart").status_code == 200 and restarts == [1]


def test_the_checks_say_what_works_and_what_to_fix(tmp_path):
    c, jf, _, _ = setup(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"jellyfin_url": "http://jf:8096"}))
    first = {r["name"]: r for r in c.get("/api/settings/checks").json()}
    assert first["Jellyfin"]["ok"] is False and "Sign in" in first["Jellyfin"]["fix"]
    assert first["Provider links"]["ok"] is None and first["Streaming apps"]["ok"] is None  # optional, not set up
    (config / "jellyfin-api-key").write_text(KEY)
    jf.libraries = [{"Name": "Movies"}]
    now = {r["name"]: r for r in c.get("/api/settings/checks").json()}
    assert now["Jellyfin"] == {
        "card": "jellyfin",
        "name": "Jellyfin",
        "ok": True,
        "detail": "http://jf:8096, 1 libraries",
        "fix": "",
    }


FAKE_RCLONE = """#!/bin/sh
# rclone, as far as the off-site setup uses it: listing the bucket answers by the key, obscure echoes
case "$1" in
  lsf) [ "$RCLONE_CONFIG_CHECK_ACCOUNT" = "good-id" ] || { echo "401 unauthorized" >&2; exit 1; } ;;
  obscure) cat ;;
esac
"""


def test_off_site_backups_are_set_up_once_per_mac_then_turned_on_here(tmp_path, monkeypatch):
    c, *_ = setup(tmp_path)
    rclone = tmp_path / "rclone"
    rclone.write_text(FAKE_RCLONE)
    rclone.chmod(0o755)
    shared = tmp_path / "rclone-config" / "backup.conf"
    monkeypatch.setattr("jobkit.offsite.CONFIG", shared)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (tmp_path / "backups").mkdir()
    (config / "settings.json").write_text(json.dumps({"rclone": str(rclone)}))
    assert c.get("/api/settings").json()["offsite"] == {"configured": False, "where": "", "on": False}
    assert c.put("/api/settings/offsite", json={"on": True}).status_code == 409  # no storage yet

    key = {"bucket": "my-bucket", "key_id": "bad-id", "key": "k-secret"}
    assert c.post("/api/settings/offsite/key", json=key).status_code == 400 and not shared.exists()
    code = c.post("/api/settings/offsite/key", json=key | {"key_id": "good-id"}).json()["recovery_code"]
    assert len(code) > 30 and shared.stat().st_mode & 0o077 == 0  # the key is in an owner-only file
    assert c.post("/api/settings/offsite/key", json=key | {"key_id": "good-id"}).status_code == 400  # once per Mac

    assert c.put("/api/settings/offsite", json={"on": True}).status_code == 409  # the backup folder comes first
    assert c.put("/api/settings/backup", json={"dir": str(tmp_path / "backups")}).status_code == 200
    assert c.put("/api/settings/offsite", json={"on": True}).json()["on"] is True
    cfg = json.loads((config / "settings.json").read_text())
    assert cfg["offsite"] == "backup:mimir-tv" and cfg["rclone_config"] == str(shared)
    stored = (config / "settings.json").read_text()
    assert "k-secret" not in stored and code not in stored  # neither the key nor the code is kept here
    assert c.put("/api/settings/offsite", json={"on": False}).json()["on"] is False

    lan = TestClient(c.app, client=("192.0.2.30", 5000))
    assert lan.post("/api/settings/offsite/key", json=key).status_code == 403


FAKE_TAILSCALE = """#!/bin/sh
# tailscale: signed in as mac.tail1234.ts.net; what it serves lives in a file next to it
STATE="$(dirname "$0")/served.json"
[ -f "$STATE" ] || echo '{}' > "$STATE"
case "$1 $2" in
  "status --json") echo '{"BackendState": "Running", "Self": {"DNSName": "mac.tail1234.ts.net."}}' ;;
  "serve status") cat "$STATE" ;;
  "serve --bg") /usr/bin/python3 -c 'import json,sys; s,p,t=sys.argv[1],sys.argv[2].split("=")[1],sys.argv[3]; d=json.load(open(s)); d.setdefault("Web",{})["mac.tail1234.ts.net:"+p]={"Handlers":{"/":{"Proxy":t}}}; json.dump(d,open(s,"w"))' "$STATE" "$3" "$4" ;;
  serve\ --https=*) /usr/bin/python3 -c 'import json,sys; s,p=sys.argv[1],sys.argv[2].split("=")[1]; d=json.load(open(s)); d.get("Web",{}).pop("mac.tail1234.ts.net:"+p,None); json.dump(d,open(s,"w"))' "$STATE" "$2" ;;
esac
"""


def test_phones_reach_it_on_the_macs_tailscale_name_and_the_tv_on_the_home_network(tmp_path, monkeypatch):
    c, *_ = setup(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "tailscale").write_text(FAKE_TAILSCALE)
    (bin_dir / "tailscale").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr("mimir_tv.wizard.lan_address", lambda: "192.0.2.10")
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "settings.json").write_text("{}")

    got = c.get("/api/settings").json()
    assert got["tailnet"] == {
        "name": "mac.tail1234.ts.net",
        "port": 8726,
        "on": False,
        "url": "https://mac.tail1234.ts.net:8726",
    }
    assert got["resolver"] == "http://192.0.2.10:8724" and got["tv_url"] == "http://192.0.2.10:8723"  # no domain

    assert c.put("/api/settings/tailnet", json={"on": True}).json()["on"] is True
    served = json.loads((bin_dir / "served.json").read_text())["Web"]
    assert served == {"mac.tail1234.ts.net:8726": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8723"}}}}
    qr = c.get("/api/settings/tailnet/qr.svg")
    assert qr.headers["content-type"].startswith("image/svg") and b"<svg" in qr.content
    # Tailscale names are this app's own names too (no hosts file needed).
    assert c.get("/api/settings", headers={"Host": "mac.tail1234.ts.net:8726"}).status_code == 200

    lan = TestClient(c.app, client=("192.0.2.30", 5000))
    assert lan.put("/api/settings/tailnet", json={"on": False}).status_code == 403
    assert c.put("/api/settings/tailnet", json={"on": False}).json()["on"] is False

    # Another app already on that port: never taken over.
    (bin_dir / "served.json").write_text(
        json.dumps({"Web": {"mac.tail1234.ts.net:8726": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:2283"}}}}})
    )
    assert c.put("/api/settings/tailnet", json={"on": True}).status_code == 409

    # With a home domain, the TV opens mimir-tv by the resolver's name.
    (config / "settings.json").write_text(json.dumps({"resolver": "https://lan.home.example.com"}))
    assert c.get("/api/settings").json()["tv_url"] == "https://lan.home.example.com"
