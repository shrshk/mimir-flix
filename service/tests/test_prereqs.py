import subprocess
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimir_flix import prereqs


def setup(monkeypatch, have: set[str]):
    """A Mac with only `have` on it (brew, uv); Jellyfin not answering; Tailscale connected."""
    monkeypatch.setattr(prereqs, "which", lambda name: f"/opt/homebrew/bin/{name}" if name in have else None)
    monkeypatch.setattr(prereqs, "check_tailscale", lambda: (True, "connected"))
    ran = []

    def runner(cmd):
        ran.append(cmd)
        have.add(cmd[-1])  # brew install uv: uv is there now
        return subprocess.CompletedProcess(cmd, 0, "installed", "")

    app = FastAPI()
    app.include_router(prereqs.router("http://127.0.0.1:9", runner))
    here = TestClient(app, client=("127.0.0.1", 5000))  # the page opened on the Mac itself
    return here, ran


def wait_done(c, key):
    for _ in range(50):
        part = next(p for p in c.get("/api/prereqs").json()["parts"] if p["key"] == key)
        if not part["installing"].get("running"):
            return part
        time.sleep(0.05)
    raise AssertionError("install did not finish")


def test_lists_what_is_missing_and_installs_only_on_a_yes_from_the_mac(monkeypatch):
    c, ran = setup(monkeypatch, {"brew"})
    parts = {p["key"]: p for p in c.get("/api/prereqs").json()["parts"]}
    assert [k for k, p in parts.items() if not p["ok"]] == ["uv", "jellyfin"]
    assert parts["uv"]["install"] == "brew install uv" and parts["brew"]["link"] == "https://brew.sh"
    assert ran == []  # looking installs nothing

    # Through Caddy (a phone on the tailnet) or from another machine: refused.
    assert c.post("/api/prereqs/uv/install", headers={"X-Forwarded-For": "100.64.0.2"}).status_code == 403
    phone = TestClient(c.app, client=("192.0.2.30", 5000))
    assert phone.post("/api/prereqs/uv/install").status_code == 403
    assert ran == []

    assert c.post("/api/prereqs/uv/install").status_code == 202
    assert wait_done(c, "uv")["ok"] is True and ran == [["brew", "install", "uv"]]
    assert c.post("/api/prereqs/uv/install").status_code == 409  # there now
    assert c.post("/api/prereqs/brew/install").status_code == 404  # Homebrew: only a link to follow


def test_nothing_installs_before_homebrew(monkeypatch):
    c, ran = setup(monkeypatch, set())
    assert c.post("/api/prereqs/jellyfin/install").json()["detail"] == "Install Homebrew first"
    assert ran == []
