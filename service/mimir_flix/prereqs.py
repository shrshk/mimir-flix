"""What this Mac needs before mimir-flix can be set up (D9 step 1): each part checked, with what it is for and how
to get it. Nothing is installed without a yes: the Setup page (/setup) shows the list and installs one part only
when its button is pressed and confirmed, and only from the Mac itself (installing software is not something a
phone on the tailnet may start). Homebrew needs the Mac's password, so its line only tells how.
"""

import json
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

PAGE = Path(__file__).parent / "static" / "setup.html"
BREW_DIRS = ["/opt/homebrew/bin", "/usr/local/bin"]


def which(name: str) -> str | None:
    return shutil.which(name) or shutil.which(name, path=":".join(BREW_DIRS))


def run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def check_brew() -> tuple[bool, str]:
    path = which("brew")
    return (True, path) if path else (False, "not installed")


def check_uv() -> tuple[bool, str]:
    path = which("uv")
    return (True, path) if path else (False, "not installed")


def check_jellyfin(url: str) -> Callable[[], tuple[bool, str]]:
    def check() -> tuple[bool, str]:
        try:
            info = httpx.get(f"{url}/System/Info/Public", timeout=4).json()
            return True, f"version {info.get('Version')} at {url}"
        except (httpx.HTTPError, ValueError):
            installed = Path("/Applications/Jellyfin.app").exists()
            return False, "installed but not running (open the Jellyfin app)" if installed else "not installed"

    return check


def check_tailscale() -> tuple[bool, str]:
    cli = which("tailscale") or (
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale" if Path("/Applications/Tailscale.app").exists() else None
    )
    if not cli:
        return False, "not installed"
    try:
        state = json.loads(run([cli, "status", "--json"]).stdout or "{}").get("BackendState")
    except (OSError, subprocess.SubprocessError, ValueError):
        state = None
    return (True, "connected") if state == "Running" else (False, f"installed, not connected ({state or 'unknown'})")


def parts(jellyfin_url: str) -> list[dict]:
    """Each part: what it is, why, how to check it, and how to install it (a brew command, or a link to follow)."""
    return [
        {
            "key": "brew",
            "name": "Homebrew",
            "why": "Installs the other parts.",
            "check": check_brew,
            "link": "https://brew.sh",  # its installer asks for the Mac's password: run it in Terminal
        },
        {
            "key": "uv",
            "name": "uv",
            "why": "Runs mimir-flix (Python).",
            "check": check_uv,
            "install": ["brew", "install", "uv"],
        },
        {
            "key": "jellyfin",
            "name": "Jellyfin",
            "why": "Plays your videos and keeps track of what was watched.",
            "check": check_jellyfin(jellyfin_url),
            "install": ["brew", "install", "--cask", "jellyfin"],
        },
        {
            "key": "tailscale",
            "name": "Tailscale",
            "why": "Reaches mimir-flix from your phone away from home (optional at home).",
            "check": check_tailscale,
            "install": ["brew", "install", "--cask", "tailscale"],
            "after": "Open Tailscale and sign in.",
            "optional": True,
        },
    ]


def from_this_mac(request: Request) -> bool:
    """Straight from this Mac: loopback, and not relayed by Caddy (which adds X-Forwarded-For)."""
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1") and "x-forwarded-for" not in request.headers


def router(jellyfin_url: str, runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> APIRouter:
    api = APIRouter()
    found = {p["key"]: p for p in parts(jellyfin_url)}
    installing: dict[str, dict] = {}  # key -> {"running", "ok", "log"}
    lock = threading.Lock()

    def install_run(cmd: list[str]) -> subprocess.CompletedProcess:
        brew = which("brew")
        return run([brew, *cmd[1:]] if brew and cmd[0] == "brew" else cmd, timeout=1800)

    runner = runner or install_run

    @api.get("/setup")
    def page():
        return FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    @api.get("/api/prereqs")
    def status(request: Request):
        out = []
        for p in found.values():
            ok, detail = p["check"]()
            out.append(
                {
                    "key": p["key"],
                    "name": p["name"],
                    "why": p["why"],
                    "ok": ok,
                    "detail": detail,
                    "optional": p.get("optional", False),
                    "install": " ".join(p["install"]) if p.get("install") else "",
                    "link": p.get("link", ""),
                    "after": p.get("after", ""),
                    "installing": installing.get(p["key"], {}),
                }
            )
        return {"parts": out, "here": from_this_mac(request)}

    @api.post("/api/prereqs/{key}/install", status_code=202)
    def install(key: str, request: Request):
        """Installs one missing part, only when asked from this Mac (the page confirms first)."""
        if not from_this_mac(request):
            raise HTTPException(403, "Install from the Mac itself (open http://127.0.0.1 on it)")
        p = found.get(key)
        if not p or not p.get("install"):
            raise HTTPException(404, "nothing to install for that")
        if key != "brew" and not which("brew"):
            raise HTTPException(409, "Install Homebrew first")
        if p["check"]()[0]:
            raise HTTPException(409, f"{p['name']} is already there")
        with lock:
            if installing.get(key, {}).get("running"):
                raise HTTPException(409, "already installing")
            installing[key] = {"running": True, "ok": None, "log": ""}

        def work():
            try:
                r = runner(p["install"])
                result = {"ok": r.returncode == 0, "log": (r.stdout + r.stderr)[-2000:]}
            except (OSError, subprocess.SubprocessError) as e:
                result = {"ok": False, "log": str(e)}
            installing[key] = {"running": False} | result

        threading.Thread(target=work, daemon=True).start()
        return {}

    return api
