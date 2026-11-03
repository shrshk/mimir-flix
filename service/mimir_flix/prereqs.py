"""What this Mac needs before mimir-flix can be set up (D9 step 1): each part checked, with what it is for and how
to get it. Nothing is installed without a yes: the Setup page (/setup) shows the list and installs one part only
when its button is pressed and confirmed, and only from the Mac itself (installing software is not something a
phone on the tailnet may start). Homebrew needs the Mac's password, so its line only tells how. The checks and the
installer are jobkit's (shared by the mimir apps); this file adds Jellyfin and the routes.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from jobkit import prereqs as shared

PAGE = Path(__file__).parent / "static" / "setup.html"


def check_jellyfin(url: str) -> Callable[[], tuple[bool, str]]:
    def check() -> tuple[bool, str]:
        try:
            info = httpx.get(f"{url}/System/Info/Public", timeout=4).json()
            return True, f"version {info.get('Version')} at {url}"
        except (httpx.HTTPError, ValueError):
            installed = Path("/Applications/Jellyfin.app").exists()
            return False, "installed but not running (open the Jellyfin app)" if installed else "not installed"

    return check


def parts(jellyfin_url: str) -> list[shared.Part]:
    jellyfin = shared.Part(
        "jellyfin",
        "Jellyfin",
        "Plays your videos and keeps track of what was watched.",
        check_jellyfin(jellyfin_url),
        ["brew", "install", "--cask", "jellyfin"],
    )
    rclone = shared.rclone()
    rclone.optional = True  # only for the off-site copy of the backups
    return [shared.brew(), shared.uv(), jellyfin, rclone, shared.tailscale(optional=True)]


def from_this_mac(request: Request) -> bool:
    """Straight from this Mac: loopback, and not relayed by Caddy (which adds X-Forwarded-For)."""
    return shared.local(request.client.host if request.client else "", "x-forwarded-for" in request.headers)


def router(jellyfin_url: str, runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> APIRouter:
    api = APIRouter()
    installer = shared.Installer(parts(jellyfin_url), runner)

    @api.get("/setup")
    def page():
        return FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    @api.get("/api/prereqs")
    def status(request: Request):
        return {"parts": installer.report(), "here": from_this_mac(request)}

    @api.post("/api/prereqs/{key}/install", status_code=202)
    def install(key: str, request: Request):
        """Installs one missing part, only when asked from this Mac (the page confirms first)."""
        if not from_this_mac(request):
            raise HTTPException(403, "Install from the Mac itself (open http://127.0.0.1 on it)")
        try:
            installer.start(key)
        except LookupError as e:
            raise HTTPException(404, "nothing to install for that") from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {}

    return api
