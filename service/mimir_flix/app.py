"""mimir-flix web app: Home, search and My list across the home library (Jellyfin), the streaming apps and any
provider plugins (catalog_routes.py), and the Library page: what Jellyfin shows, managed from one place.

- Libraries: create, rename, delete. Folders in a library: add, remove; everything under a folder is included.
- Exclude a subfolder: writes an empty `.ignore` file there, which Jellyfin skips (include it again to remove it).
- Single videos: linked into the library's own "picks" folder, so one file can be added without its folder.
- Scan now, with live status: the running scan's percent and each library's item count.
Settings: settings.py (one settings.json); listens on 127.0.0.1 unless it says otherwise."""

import hashlib
import ipaddress
import os
import secrets
import sqlite3
import tarfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import uvicorn
from jobkit import Jobs
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from mimir_flix.jellyfin import Jellyfin, JellyfinError
from mimir_flix.jf_routes import router as jf_router
from mimir_flix.kodi import Kodi
from mimir_flix.mcp_server import router as mcp_router
from mimir_flix.prereqs import router as prereqs_router
from mimir_flix.recs import Recs
from mimir_flix.recs import router as recs_router
from mimir_flix.resolver import start_resolver
from mimir_flix.catalog import CatalogError, Catalogs
from mimir_flix.catalog_routes import hidden_libraries, listing, plugin_paths
from mimir_flix.catalog_routes import router as catalog_router
from mimir_flix import backup
from mimir_flix import jobs as app_jobs
from mimir_flix import settings
from mimir_flix.streaming import Streaming, pick
from mimir_flix.streaming import router as streaming_router
from mimir_flix.taste import Taste
from mimir_flix.taste import router as taste_router
from mimir_flix.tv import Tv, TvError
from mimir_flix.tvmessage import Overlay
from mimir_flix.tvmessage import router as tvmessage_router
from mimir_flix.providers import Providers
from mimir_flix.providers import router as providers_router
from mimir_flix.vault import Vault
from mimir_flix.watchlist import Watchlist
from mimir_flix.watchlist import router as list_router
from mimir_flix.wizard import allowed
from mimir_flix.wizard import router as wizard_router

STATIC = Path(__file__).parent / "static"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts", ".3gp", ".insv", ".strm"}
KINDS = Literal["movies", "tvshows", "homevideos", "music", "mixed"]


class NewLibrary(BaseModel):
    name: str = Field(min_length=1)
    kind: KINDS = "homevideos"
    folders: list[str] = []


class Rename(BaseModel):
    name: str = Field(min_length=1)


class PathBody(BaseModel):
    path: str = Field(min_length=1)


def known_host(host: str, names: list[str]) -> bool:
    """A Host header this app answers to: an address, localhost, a .local or tailnet name, or one of `names`. Any
    other name is a web page that pointed its own name at this Mac (DNS rebinding)."""
    try:
        name = urlsplit("//" + host).hostname or ""  # without the port; [::1] as ::1
    except ValueError:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or name.endswith((".local", ".ts.net")) or name in {n.lower() for n in names}


def create_app(
    jf: Jellyfin,
    picks_root: Path,
    *routers,
    connectors: dict | None = None,
    other_paths: Callable | None = None,
    hosts: list[str] | None = None,
) -> FastAPI:
    """other_paths(path, request): answers a GET for a path nothing here uses (the provider plugins' own paths), or
    None for a 404. hosts: the names it is reached by besides addresses, .local and tailnet names (None: any)."""
    app = FastAPI(title="mimir-flix")

    @app.middleware("http")
    async def same_site(request: Request, call_next):
        host = request.headers.get("host", "")
        if hosts is not None and not known_host(host, hosts):
            return PlainTextResponse("Unknown host name: add it to hosts in settings.json", 421)
        # A browser names the page a request comes from; one from another site may not change anything here.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None and urlsplit(origin).netloc != host:
            return PlainTextResponse("Refused: a request from another site", 403)
        response = await call_next(request)
        # Never inside another site's frame: a click on Play, Pause or Run now made under a decoy page.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        return response
    for r in routers:
        app.include_router(r)

    @app.get("/api/connectors")
    def which():
        """What is set up here (providers, Kodi, the TV): the page leaves out what is not. A callable value is read
        on each call (providers added on the Settings page show at once)."""
        return {k: v() if callable(v) else v for k, v in (connectors or {}).items()}

    def call(fn, *args):
        try:
            return fn(*args)
        except JellyfinError as e:
            raise HTTPException(502, str(e)) from e

    def library(name: str) -> dict:
        lib = next((x for x in call(jf.libraries) if x["Name"] == name), None)
        if not lib:
            raise HTTPException(404, "no such library")
        return lib

    # Finding .ignore files walks the whole folder tree (thousands of files on a drive), so it is remembered for
    # 10 minutes and forgotten whenever an exclusion changes here.
    excluded_cache: dict[Path, tuple[float, list[str]]] = {}

    def exclusions(root: Path) -> list[str]:
        hit = excluded_cache.get(root)
        if hit and hit[0] > time.time():
            return hit[1]
        found = sorted(str(i.parent) for i in root.rglob(".ignore")) if root.is_dir() else []
        excluded_cache[root] = (time.time() + 600, found)
        return found

    def picks_dir(name: str) -> Path:
        return picks_root / name

    def covering(lib: dict, path: Path) -> bool:
        return any(path == Path(p) or Path(p) in path.parents for p in lib["Locations"])

    @app.get("/")
    def index():
        # Always checked again (a 304 when unchanged): the iPhone's Home Screen app otherwise keeps an old page.
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/version")
    def version():
        """The page's version: the Home Screen app reloads when it comes back and this has changed."""
        return {"page": hashlib.sha256((STATIC / "index.html").read_bytes()).hexdigest()[:12]}

    @app.get("/tv/app.apk")
    def tv_app():
        """The TV app (repo mimir-flix-tv), for the TV's Downloader app: put its built APK in the data folder's tv/."""
        apk = settings.tv_apk_path()
        if not apk.exists():
            raise HTTPException(
                404, "No TV app here yet: build mimir-flix-tv and copy out/mimir-flix-tv.apk to " + str(apk)
            )
        return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="mimir-flix-tv.apk")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon-{size}.png")
    def icon(size: Literal["32", "180", "192", "512"]):
        return FileResponse(STATIC / f"icon-{size}.png", media_type="image/png")

    @app.get("/fonts/{name}")
    def font(name: Literal["caveat.woff2", "patrick-hand-400.woff2"]):
        """The Doodle theme's hand-drawn fonts, served from here so no page asks Google for them."""
        return FileResponse(
            STATIC / "fonts" / name, media_type="font/woff2", headers={"Cache-Control": "max-age=2592000"}
        )

    @app.get("/api/libraries")
    def libraries():
        out = []
        for lib in call(jf.libraries):
            picks = picks_dir(lib["Name"])
            folders = []
            for p in lib["Locations"]:
                if Path(p) == picks:
                    continue
                root = Path(p)
                excluded = exclusions(root)
                folders.append({"path": p, "available": root.is_dir(), "excluded": excluded})
            videos = [
                {"name": f.name, "target": os.readlink(f), "available": f.exists()}
                for f in sorted(picks.iterdir() if picks.is_dir() else [])
                if f.is_symlink()
            ]
            out.append(
                {
                    "name": lib["Name"],
                    "kind": lib.get("CollectionType") or "mixed",
                    "id": lib.get("ItemId"),
                    "folders": folders,
                    "videos": videos,
                    "items": call(jf.count, lib["ItemId"]) if lib.get("ItemId") else 0,
                    "refresh": lib.get("RefreshStatus"),
                    "progress": lib.get("RefreshProgress"),
                }
            )
        return out

    @app.get("/api/status")
    def status():
        return {"scan": call(jf.scan_task)}

    @app.post("/api/libraries", status_code=201, dependencies=[Depends(allowed)])
    def create(body: NewLibrary):
        if any(x["Name"] == body.name for x in call(jf.libraries)):
            raise HTTPException(409, "a library with that name exists")
        for f in body.folders:
            if not Path(f).is_dir():
                raise HTTPException(422, f"not a folder: {f}")
        call(jf.create_library, body.name, body.kind, body.folders)
        return {"name": body.name}

    @app.put("/api/libraries/{name}", dependencies=[Depends(allowed)])
    def rename(name: str, body: Rename):
        library(name)
        call(jf.rename_library, name, body.name)
        old, new = picks_dir(name), picks_dir(body.name)
        if old.is_dir() and not new.exists():
            old.rename(new)
            call(jf.remove_path, body.name, str(old))
            call(jf.add_path, body.name, str(new))
        return {"name": body.name}

    @app.delete("/api/libraries/{name}", status_code=204, dependencies=[Depends(allowed)])
    def delete(name: str):
        """Removes the library from Jellyfin. The files themselves are never touched."""
        library(name)
        call(jf.delete_library, name)
        picks = picks_dir(name)
        if picks.is_dir():
            for f in picks.iterdir():
                if f.is_symlink():
                    f.unlink()
            picks.rmdir()

    @app.post("/api/libraries/{name}/folders", status_code=201, dependencies=[Depends(allowed)])
    def add_folder(name: str, body: PathBody):
        lib = library(name)
        path = Path(body.path)
        if not path.is_dir():
            raise HTTPException(422, "not a folder")
        if covering(lib, path):
            raise HTTPException(409, "already included (it or a parent folder is in this library)")
        # A folder that holds folders already in the library replaces them.
        for p in lib["Locations"]:
            if path in Path(p).parents and Path(p) != picks_dir(name):
                call(jf.remove_path, name, p)
        call(jf.add_path, name, str(path))
        return {"path": str(path)}

    @app.delete("/api/libraries/{name}/folders", status_code=204, dependencies=[Depends(allowed)])
    def remove_folder(name: str, path: str):
        lib = library(name)
        if path not in lib["Locations"]:
            raise HTTPException(404, "not a folder of this library")
        call(jf.remove_path, name, path)

    @app.post("/api/libraries/{name}/exclude", status_code=201, dependencies=[Depends(allowed)])
    def exclude(name: str, body: PathBody):
        """Leaves a subfolder out of the library (an empty .ignore file there)."""
        lib = library(name)
        path = Path(body.path)
        if not path.is_dir() or not covering(lib, path) or str(path) in lib["Locations"]:
            raise HTTPException(422, "pick a subfolder inside one of this library's folders")
        (path / ".ignore").touch()
        excluded_cache.clear()
        return {"path": str(path)}

    @app.delete("/api/libraries/{name}/exclude", status_code=204, dependencies=[Depends(allowed)])
    def include_again(name: str, path: str):
        lib = library(name)
        marker = Path(path) / ".ignore"
        if not covering(lib, Path(path)) or not marker.is_file() or marker.stat().st_size:
            raise HTTPException(404, "no exclusion there")
        marker.unlink()
        excluded_cache.clear()

    @app.post("/api/libraries/{name}/videos", status_code=201, dependencies=[Depends(allowed)])
    def add_video(name: str, body: PathBody):
        lib = library(name)
        video = Path(body.path)
        if not video.is_file() or video.suffix.lower() not in VIDEO_EXTS:
            raise HTTPException(422, "not a video file")
        picks = picks_dir(name)
        picks.mkdir(parents=True, exist_ok=True)
        link = picks / video.name
        if link.is_symlink() or link.exists():
            if link.is_symlink() and os.readlink(link) == str(video):
                raise HTTPException(409, "already in this library")
            tag = hashlib.sha256(str(video).encode()).hexdigest()[:6]
            link = picks / f"{video.stem} ({tag}){video.suffix}"
        link.symlink_to(video)
        if str(picks) not in lib["Locations"]:
            call(jf.add_path, name, str(picks))
        else:
            call(jf.scan, lib.get("ItemId"))
        return {"name": link.name}

    @app.delete("/api/libraries/{name}/videos/{file}", status_code=204, dependencies=[Depends(allowed)])
    def remove_video(name: str, file: str):
        lib = library(name)
        link = picks_dir(name) / file
        if not link.is_symlink() or link.parent != picks_dir(name):
            raise HTTPException(404, "not a picked video of this library")
        link.unlink()
        call(jf.scan, lib.get("ItemId"))

    @app.post("/api/libraries/{name}/scan", status_code=202, dependencies=[Depends(allowed)])
    def scan(name: str):
        call(jf.scan, library(name).get("ItemId"))
        return {}

    @app.post("/api/scan", status_code=202, dependencies=[Depends(allowed)])
    def scan_all():
        call(jf.scan)
        return {}

    @app.get("/api/browse", dependencies=[Depends(allowed)])
    def browse(path: str = "/Volumes"):
        """Folders and video files under path, for picking what goes into a library."""
        p = Path(path).expanduser()
        if not p.is_dir():
            raise HTTPException(404, "not a folder")
        try:
            entries = sorted(p.iterdir(), key=lambda c: c.name.lower())
        except PermissionError as e:
            raise HTTPException(403, "no access") from e
        dirs = [c.name for c in entries if c.is_dir() and not c.name.startswith(".")]
        videos = [
            {"name": c.name, "bytes": c.stat().st_size}
            for c in entries
            if c.is_file() and c.suffix.lower() in VIDEO_EXTS and not c.name.startswith("._")
        ]
        return {"path": str(p), "parent": str(p.parent), "dirs": dirs, "videos": videos}

    if other_paths:  # last, so it never hides a route of mimir-flix itself

        @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        def other(path: str, request: Request):
            found = other_paths(path, request)
            if found is None:
                raise HTTPException(404)
            return found

    return app


def main() -> None:
    config = settings.config_dir()
    cfg = settings.load(config)
    data = Path(os.environ.get("MIMIR_FLIX_DATA", Path.home() / "Library/Application Support/mimir-flix"))
    data.mkdir(parents=True, exist_ok=True)
    key_file = config / "jellyfin-api-key"
    jf = Jellyfin(cfg["jellyfin_url"], key_file.read_text().strip() if key_file.exists() else "")
    picks = data / "picks"

    # Every login taken in (providers, Kodi) is kept encrypted in the app's database; the key is in the Keychain.
    vault = Vault.for_mac(data / "mimir-flix.db", config)
    kodi_pw = settings.kodi_password(cfg["kodi_password_file"], vault) if cfg["kodi_url"] else ""
    kodi = Kodi(cfg["kodi_url"], kodi_pw) if kodi_pw else None
    # Where Jellyfin's .strm files point: an address every player can reach, ideally https (Safari will not play
    # an http link inside the https Jellyfin page). The resolver itself listens on a port of this machine.
    port = cfg["resolver_port"]
    resolver = cfg["resolver"] or f"http://127.0.0.1:{port}"
    media_root = Path(cfg["media_root"])
    # 📺 TV opens the Jellyfin TV app when it is closed (the TV trusts this machine's ADB key: Pair on the Settings page).
    tv = Tv(settings.default_tv(cfg), config / "adbkey") if settings.default_tv(cfg) else None
    open_app = tv.open_jellyfin if tv else None
    # Provider plugins (catalog.py): the ones in Settings whose plugin is installed.
    catalogs = Catalogs(config, vault, data)
    # My list, for every house: streaming titles, library titles and providers' shows and videos.
    watchlist = Watchlist(data / "mimir-flix.db")
    routers = [jf_router(jf, kodi, resolver, open_app, watchlist, hidden=lambda: hidden_libraries(catalogs))]
    # Where titles stream, filtered to the house's apps: TMDB, with its API Read Access Token (from your password manager) in
    # this file. Without it, search shows only Jellyfin (and the providers).
    token_file = config / "tmdb-token"
    streaming = Streaming(
        token_file.read_text().strip() if token_file.exists() else "",
        data / "mimir-flix.db",
        pick(cfg["streaming_apps"]),
        cfg["streaming_region"],
    )
    routers.append(streaming_router(streaming, tv.open_app if tv else None))

    def watched() -> set[str]:
        try:
            return jf.watched_titles()
        except JellyfinError:
            return set()  # Jellyfin not answering: recommend without leaving anything out

    # Recommendations (E2): categories from TMDB, filtered by the person's apps, language, genre and more, under the
    # house's rules (Settings) and without what was watched or given 👎. The taste and the pool are there for an
    # assistant that recommends (E2-2: an OpenClaw skill), which shows its picks in its own chat.
    recs = Recs(streaming, watched, rules=lambda: settings.recs_rules(config), disliked=lambda: taste.disliked())
    taste = Taste(data / "mimir-flix.db", recs, jf if key_file.exists() else None)
    routers += [recs_router(recs), taste_router(taste), list_router(watchlist, recs.details)]
    # Messages to the TV (a doodle or a photo): memory only; the TV app's overlay starts over ADB when one comes.
    routers.append(tvmessage_router(Overlay(wake=tv_try(tv.open_overlay) if tv else None)))
    # Each run is recorded in mimir-flix's own database (jobkit); the jobs page reads GET /api/jobs.
    jobs = Jobs(data / "mimir-flix.db", "mimir-flix")
    routers.append(app_jobs.router(jobs))
    start_backups(jobs, data, config)
    # The MCP endpoint for an assistant (OpenClaw), behind its own token (D8).
    routers.append(mcp_router(mcp_token(config / "mcp-token"), tv.now if tv else None))
    # What this Mac needs (D9): the Setup page checks each part and installs one only when asked on the Mac itself.
    routers.append(prereqs_router(cfg["jellyfin_url"]))
    # Then setup itself on the same page, one step at a time; it locks once every step is done or skipped.
    routers.append(wizard_router(config, data / "mimir-flix.db", vault, catalogs=catalogs))
    # Providers (M2, O1): IPTV played by Jellyfin's Live TV from mimir-flix's local playlist, and plugin providers;
    # logins stay in the vault.
    iptv = Providers(
        config, vault, lambda: jf if key_file.exists() else None, cfg["port"], catalogs=catalogs, base=resolver
    )
    routers.append(providers_router(iptv, allowed))
    # A provider plugin's pages, the resolver its .strm files point at, and the hourly jobs.
    start_resolver(catalogs, port, jf=jf)
    routers.append(
        catalog_router(catalogs, jf, kodi, media_root, resolver, data / "thumbs", open_app, watchlist=watchlist)
    )
    start_provider_jobs(jobs, catalogs, jf, media_root, resolver, watchlist)
    connectors = {"providers": lambda: listing(catalogs), "kodi": kodi is not None, "tv": tv is not None}
    names = [*cfg["hosts"], urlsplit(resolver).hostname or ""]  # the TV reaches this app by the resolver's name
    app = create_app(
        jf, picks, *routers, connectors=connectors, other_paths=plugin_paths(catalogs, resolver), hosts=names
    )
    # No access log: query strings carry signed links and, for a plugin's own API, its login.
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], access_log=False)


def mcp_token(path: Path) -> str:
    """The assistant's token: made on first start, owner-only; the assistant's config reads it from this file."""
    if not path.exists():
        old = os.umask(0o077)
        try:
            path.write_text(secrets.token_urlsafe(32))
        finally:
            os.umask(old)
    return path.read_text().strip()


def tv_try(fn):
    """A TV call from a background thread: a TV that is off or not answering is reported, never raised."""

    def run():
        try:
            fn()
        except TvError as e:
            print("tv:", e)

    return run


def start_provider_jobs(jobs: Jobs, catalogs: Catalogs, jf, media_root: Path, resolver: str, watchlist) -> None:
    """Hourly: new episodes of followed and listed shows (from the second hour), and each provider's own jobs when
    they are due (from the start), unless a job is paused on the jobs page."""
    app_jobs.define(jobs)

    def new_episodes():
        try:
            app_jobs.new_episodes(jobs, catalogs, jf, media_root, resolver, watchlist)
        except (JellyfinError, CatalogError, OSError) as e:  # recorded as failed; tried again next round
            print("new episodes failed:", e)

    jobs.register(app_jobs.NEW_EPISODES, new_episodes)

    def hourly():
        first = True
        while True:
            for name, job in app_jobs.provider_jobs(jobs, catalogs):
                if not jobs.paused(name) and job.due():
                    app_jobs.run_job(jobs, name, job)
            if not first and not jobs.paused(app_jobs.NEW_EPISODES):
                new_episodes()
            first = False
            time.sleep(3600)

    threading.Thread(target=hourly, daemon=True).start()


def start_backups(jobs: Jobs, data: Path, config: Path) -> None:
    """The nightly backup (D9 step 6), at 3:30 into the folder Settings names; nothing while no folder is set."""
    jobs.define(backup.JOB, "Backup: mimir-flix and Jellyfin settings, lists and watch history", "nightly at 3:30")
    jobs.define(backup.OFFSITE, "Off-site: the backup folder to the rclone remote in settings", "after each backup")

    def nightly():
        cfg = settings.load(config)
        if not cfg["backup_dir"]:
            return
        try:
            with jobs.run(backup.JOB) as run:
                out = backup.back_up(data, config, Path(cfg["jellyfin_data"]), Path(cfg["backup_dir"]))
                run.summary = f"{out.name} ({out.stat().st_size // 1024} KB)"
        except (backup.BackupError, OSError, sqlite3.Error, tarfile.TarError) as e:  # recorded as failed by jobkit
            print("backup failed:", e)
            return  # a failed snapshot is never synced off-site
        if not jobs.paused(backup.OFFSITE):
            offsite()

    def offsite():
        cfg = settings.load(config)
        if not cfg["offsite"] or not cfg["backup_dir"]:
            return
        try:
            with jobs.run(backup.OFFSITE) as run:
                where = Path(cfg["backup_dir"])
                run.summary = backup.off_site(where, cfg["offsite"], cfg["rclone_config"], cfg["rclone"])
        except (backup.BackupError, OSError) as e:  # recorded as failed by jobkit
            print("off-site backup failed:", e)

    jobs.register(backup.JOB, nightly)
    jobs.register(backup.OFFSITE, offsite)

    def daily():
        while True:
            now = datetime.now()
            at = now.replace(hour=3, minute=30, second=0, microsecond=0)
            time.sleep(((at if at > now else at + timedelta(days=1)) - now).total_seconds())
            if not jobs.paused(backup.JOB):
                nightly()

    threading.Thread(target=daily, daemon=True).start()
