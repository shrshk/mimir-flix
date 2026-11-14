"""Setup and Settings in the browser (D9 steps 2 and 3): Jellyfin, your media, your streaming apps, the TVs and
phones (providers are added on the Settings page: providers.py). Both pages save through the same code; they differ in when they may.

- Setup (/setup, after the prerequisites): one step at a time. A step is done when its setting works or when it was
  finished or skipped there; progress is kept in mimir-tv's own database, so setup resumes where it stopped. Once
  every step is done or skipped it locks (a Mac already set up is locked from the start).
- Settings (/settings): the same parts as cards with live ✓/✗ and the fix (checks.py), changeable any time. It
  replaced mimir-tv-setup and mimir-tv-check, so there is one way to set mimir-tv up.
- Only the Mac itself or a tailnet device may change anything (not any device on the Wi-Fi).
- Passwords are used once (Jellyfin's admin) or go to the vault (providers, Kodi: encrypted in mimir-tv's database,
  the key in the Keychain); the TMDB token to an owner-only file; never to settings.json or the log.
- mimir-tv reads its settings when it starts. Setup restarts it (launchd starts it again) after the Jellyfin step,
  which the Library needs at once, and at Finish; Settings says a restart is needed and has a button for it.
"""

import os
import shutil
import socket
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

import qrcode
import qrcode.image.svg
from jobkit import offsite as shared_offsite
from jobkit import prereqs as shared_prereqs

from mimir_tv import backup, checks, settings
from mimir_tv.catalog import Catalogs
from mimir_tv.hls import from_tailnet
from mimir_tv.jellyfin import Jellyfin, JellyfinError
from mimir_tv.prereqs import from_this_mac
from mimir_tv.recs import LANGUAGES
from mimir_tv.setup import SignInRefused, jellyfin_first_run, jellyfin_key, save_secret
from mimir_tv.streaming import APPS, pick
from mimir_tv.tv import Tv, TvError
from mimir_tv.vault import Vault

SETTINGS_PAGE = Path(__file__).parent / "static" / "settings.html"
SCHEMA = """CREATE TABLE IF NOT EXISTS setup_steps (
    step TEXT PRIMARY KEY,  -- a step's key, or 'finished' once the wizard is locked
    state TEXT NOT NULL,    -- done | skipped
    at REAL NOT NULL
)"""

STEPS = [
    {"key": "jellyfin", "name": "Jellyfin", "optional": False},
    {"key": "media", "name": "Your media", "optional": True},
    {"key": "streaming", "name": "Your streaming apps", "optional": True},
    {"key": "tv", "name": "The TV", "optional": True},
    {"key": "phones", "name": "Phones", "optional": True},
    {"key": "backup", "name": "Backups", "optional": True},
]
OFFSITE_TO = "backup:mimir-tv"  # this app's folder in the Mac's shared off-site storage (jobkit.offsite)


class OffsiteKey(BaseModel):
    bucket: str = Field(max_length=100)
    key_id: str = Field(max_length=200)
    key: str = Field(max_length=200)


class Offsite(BaseModel):
    on: bool


class BackupStep(BaseModel):
    dir: str = Field(min_length=1)
    offsite: bool = False


class JellyfinStep(BaseModel):
    url: str = Field(min_length=1)
    name: str = Field(min_length=1)
    password: str = ""


class StreamingStep(BaseModel):
    token: str = ""  # empty: keep the saved one
    apps: str = ""
    region: str = Field("US", min_length=2, max_length=2)


class TvEntry(BaseModel):
    name: str = Field("TV", min_length=1, max_length=40)
    host: str = Field(min_length=1, max_length=100)
    kind: Literal["android", "fire"] = "android"


class TvStep(BaseModel):
    """Setup's TV step: one TV (more are added on the Settings page)."""

    host: str = Field(min_length=1)
    kind: Literal["android", "fire"] = "android"
    kodi_url: str = ""
    kodi_password: str = ""


class TvsSettings(BaseModel):
    tvs: list[TvEntry] = Field(max_length=10)  # the first is the default
    kodi_url: str = ""
    kodi_password: str = ""  # empty: keep the saved one


class PhonesStep(BaseModel):
    direct: bool = False
    resolver: str = ""
    tailnet: bool = False  # phones through Tailscale (https on this Mac's Tailscale name)


class Tailnet(BaseModel):
    on: bool


def lan_address() -> str:
    """This Mac's address on the home network (the one its default route uses; nothing is sent), or ""."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # a documentation address: only picks the interface
            return s.getsockname()[0]
        except OSError:
            return ""


class RecsRules(BaseModel):
    avoid_genres: list[str] = Field([], max_length=40)
    languages: list[str] = Field([], max_length=20)


class BackupDir(BaseModel):
    dir: str = ""  # empty: backups off


class PairBody(BaseModel):
    host: str = ""  # empty: the default TV


def restart_soon() -> None:
    """Exits a moment after the reply is sent; launchd (KeepAlive) starts mimir-tv again with the new settings."""
    threading.Thread(target=lambda: (time.sleep(1), os._exit(0)), daemon=True).start()


def allowed(request: Request) -> None:
    """Changes come from this Mac or a tailnet device (Tailscale signs devices in), not any device on the Wi-Fi."""
    peer = request.client.host if request.client else ""
    if not (from_this_mac(request) or from_tailnet(peer, request.headers.get("x-forwarded-for"))):
        raise HTTPException(403, "Change settings from this Mac or a device on your tailnet")


class Progress:
    def __init__(self, db: Path):
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.db.execute(SCHEMA)
        self.db.commit()

    def states(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT step, state FROM setup_steps").fetchall())

    def mark(self, step: str, state: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO setup_steps VALUES (?, ?, ?)", (step, state, time.time()))
        self.db.commit()


def router(
    config: Path,
    db: Path,
    vault: Vault,
    transport: httpx.BaseTransport | None = None,
    restart: Callable[[], None] = restart_soon,
    catalogs: Catalogs | None = None,
) -> APIRouter:
    api = APIRouter()
    change = APIRouter(dependencies=[Depends(allowed)])
    progress = Progress(db)
    pairing: dict[str, dict] = {}  # TV address -> {"running", "ok", "detail"}
    pending = {"restart": False}  # Settings saved something mimir-tv reads only when it starts

    def jellyfin() -> Jellyfin | None:
        cfg = settings.load(config)
        key = config / "jellyfin-api-key"
        return Jellyfin(cfg["jellyfin_url"], key.read_text().strip(), transport) if key.exists() else None

    def detected(step: str) -> bool:
        """Whether the step's setting is there and works, whatever set it (Setup, Settings, by hand)."""
        cfg = settings.load(config)
        if step in ("jellyfin", "media"):
            jf = jellyfin()
            try:
                return bool(jf and jf.libraries()) if step == "media" else bool(jf and jf.libraries() is not None)
            except JellyfinError:
                return False
        if step == "streaming":
            return (config / "tmdb-token").exists()
        if step == "tv":
            return bool(cfg["tvs"])
        if step == "phones":
            return cfg["host"] == "0.0.0.0" or bool(cfg["resolver"])
        if step == "backup":
            return bool(cfg["backup_dir"])
        return False

    def defaults(cfg: dict) -> dict:
        return {
            "jellyfin_url": cfg["jellyfin_url"],
            "region": cfg["streaming_region"],
            "apps": cfg["streaming_apps"],
            "tv_host": settings.default_tv(cfg),
            "resolver": cfg["resolver"]
            or f"http://{lan_address() or socket.gethostname().removesuffix('.local') + '.local'}:{cfg['resolver_port']}",
            "backup_dir": cfg["backup_dir"],
            "offsite": offsite_status(cfg),
            "tailnet": tailnet_status(cfg),
            "tv_url": tv_url(cfg),  # the Mac's shared off-site storage (no secrets), and whether it is on
        }

    def status() -> dict:
        saved = progress.states()
        steps = []
        for s in STEPS:
            state = "done" if detected(s["key"]) or saved.get(s["key"]) == "done" else saved.get(s["key"], "todo")
            steps.append(s | {"state": state})
        cfg = settings.load(config)
        return {
            "steps": steps,
            "locked": saved.get("finished") == "done" or all(s["state"] != "todo" for s in steps),
            "defaults": defaults(cfg),
            "pairing": pairing.get(settings.default_tv(cfg), {}),
        }

    def open_only() -> None:
        if status()["locked"]:
            raise HTTPException(409, "Setup is finished; change settings on the Settings page")

    # What both pages save.
    def save_jellyfin(body: JellyfinStep) -> None:
        """Finishes Jellyfin's own first run when needed (this becomes its admin), then signs in once for
        mimir-tv's key; the password is not kept."""
        url = body.url.rstrip("/")
        try:
            with httpx.Client(timeout=5, transport=transport) as http:
                info = http.get(f"{url}/System/Info/Public").json()
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(502, f"Jellyfin is not answering at {url}") from e
        try:
            if not info.get("StartupWizardCompleted"):
                jellyfin_first_run(url, body.name, body.password, transport)
            key = jellyfin_key(url, body.name, body.password, transport)
        except SignInRefused as e:
            raise HTTPException(401, str(e)) from e
        except httpx.HTTPError as e:
            raise HTTPException(502, "Jellyfin did not accept the setup") from e
        save_secret(config / "jellyfin-api-key", key)
        settings.save({"jellyfin_url": url}, config)

    def save_streaming(body: StreamingStep) -> None:
        token_file = config / "tmdb-token"
        if not body.token and not token_file.exists():
            raise HTTPException(422, "Paste the TMDB API Read Access Token")
        try:
            pick(body.apps)
        except SystemExit as e:
            raise HTTPException(422, str(e)) from e
        if body.token:
            save_secret(token_file, body.token.strip())
        settings.save({"streaming_apps": body.apps, "streaming_region": body.region.upper()}, config)

    def save_tvs(tvs: list[TvEntry], kodi_url: str, kodi_password: str) -> None:
        hosts = [t.host.strip() for t in tvs]
        if len(set(hosts)) < len(hosts):
            raise HTTPException(422, "Each TV needs its own address")
        changes = {"tvs": [t.model_dump() | {"host": t.host.strip()} for t in tvs], "tv_host": "", "kodi_url": ""}
        if kodi_url.strip():
            if not kodi_url.strip().startswith("http"):
                raise HTTPException(422, "Kodi's address starts with http://")
            changes["kodi_url"] = kodi_url.strip()
            if kodi_password:  # into the vault; empty keeps the password there is, wherever it is
                vault.put("kodi", {"password": kodi_password})
                changes["kodi_password_file"] = "vault:kodi"
        settings.save(changes, config)

    def save_phones(body: PhonesStep) -> None:
        if body.resolver and not body.resolver.startswith(("http://", "https://")):
            raise HTTPException(422, "The address starts with http:// or https://")
        settings.save({"host": "0.0.0.0" if body.direct else "127.0.0.1", "resolver": body.resolver}, config)
        if body.tailnet:
            tailnet_on(True)

    # Phones: this Mac's Tailscale name over https (tailscale serve: Tailscale's own certificate, the tailnet only,
    # never Funnel), on a port of mimir-tv's own. The TV has no Tailscale: it uses the home network (tv_url).
    def tailnet_status(cfg: dict) -> dict:
        name, port = shared_prereqs.tailscale_name(), cfg["tailnet_port"]
        on = bool(name) and shared_prereqs.served().get(port, "").rstrip("/") == f"http://127.0.0.1:{cfg['port']}"
        return {"name": name, "port": port, "on": on, "url": f"https://{name}:{port}" if name else ""}

    def tailnet_on(on: bool) -> dict:
        cfg = settings.load(config)
        if not on:
            shared_prereqs.unserve(cfg["tailnet_port"])
        else:
            try:
                shared_prereqs.serve(cfg["tailnet_port"], f"http://127.0.0.1:{cfg['port']}")
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
        return tailnet_status(cfg)

    def tv_url(cfg: dict) -> str:
        """Where the TV app opens mimir-tv: the resolver's name when it serves mimir-tv too (a home domain), else
        this Mac's home address on mimir-tv's own port (which then listens on the home network: "direct")."""
        r = cfg["resolver"]
        parts = urlsplit(r) if r else None
        if parts and parts.port not in (cfg["resolver_port"],):
            return r.rstrip("/")
        host = (parts.hostname if parts else "") or lan_address()
        return f"http://{host}:{cfg['port']}" if host else ""

    def start_pairing(host: str) -> None:
        """Asks the TV to trust this Mac: someone accepts the prompt on the TV within 90 seconds."""
        if not host:
            raise HTTPException(409, "Save the TV's address first")
        if pairing.get(host, {}).get("running"):
            raise HTTPException(409, "already waiting for that TV")
        pairing[host] = {"running": True}

        def work():
            try:
                model = Tv(host, config / "adbkey").shell("getprop ro.product.model", auth_timeout=90).strip()
                pairing[host] = {"running": False, "ok": True, "detail": f"{model} trusts this Mac"}
            except TvError as e:
                pairing[host] = {"running": False, "ok": False, "detail": str(e)}

        threading.Thread(target=work, daemon=True).start()

    # Setup.
    @change.get("/api/setup")  # what is set up and where: for those who may change it
    def get():
        return status()

    @change.post("/api/setup/jellyfin")
    def jellyfin_step(body: JellyfinStep):
        open_only()
        save_jellyfin(body)
        progress.mark("jellyfin", "done")
        restart()
        return {"restarting": True}

    @change.post("/api/setup/media")
    def media_step():
        """The folders themselves are picked on the Library page; this step is done once a library exists."""
        open_only()
        if not detected("media"):
            raise HTTPException(409, "Add a library on the Library page first, or skip this step")
        progress.mark("media", "done")
        return {}

    @change.post("/api/setup/streaming")
    def streaming_step(body: StreamingStep):
        open_only()
        save_streaming(body)
        progress.mark("streaming", "done")
        return {}

    @change.post("/api/setup/tv")
    def tv_step(body: TvStep):
        open_only()
        save_tvs([TvEntry(host=body.host, kind=body.kind)], body.kodi_url, body.kodi_password)
        progress.mark("tv", "done")
        return {}

    @change.post("/api/setup/tv/pair", status_code=202)
    def setup_pair():
        open_only()
        start_pairing(settings.default_tv(settings.load(config)))
        return {}

    @change.post("/api/setup/phones")
    def phones_step(body: PhonesStep):
        open_only()
        save_phones(body)
        progress.mark("phones", "done")
        return {}

    @change.post("/api/setup/{step}/skip")
    def skip(step: str):
        open_only()
        found = next((s for s in STEPS if s["key"] == step), None)
        if not found:
            raise HTTPException(404, "no such step")
        if not found["optional"]:
            raise HTTPException(409, f"{found['name']} cannot be skipped")
        progress.mark(step, "skipped")
        return {}

    @change.post("/api/setup/finish")
    def finish():
        now = status()
        if progress.states().get("finished") == "done":
            raise HTTPException(409, "Setup is already finished")
        left = [s["name"] for s in now["steps"] if s["state"] == "todo"]
        if left:
            raise HTTPException(409, f"Still to do: {', '.join(left)}")
        progress.mark("finished", "done")
        restart()
        return {"restarting": True}

    # Settings.
    @api.get("/settings")
    def settings_page():
        return FileResponse(SETTINGS_PAGE, headers={"Cache-Control": "no-cache"})

    @change.get("/api/settings")  # addresses, folders, the off-site bucket: for those who may change them
    def current():
        """What is set, never a secret: whether a login or token is saved, not what it is."""
        cfg = settings.load(config)
        backups, why = backup.listed_soon(Path(cfg["backup_dir"])) if cfg["backup_dir"] else ([], "")
        return defaults(cfg) | {
            "tmdb_token": (config / "tmdb-token").exists(),
            "tvs": cfg["tvs"],
            "kodi_url": cfg["kodi_url"],
            "kodi_password": bool(settings.kodi_password(cfg["kodi_password_file"], vault)),
            "direct": cfg["host"] == "0.0.0.0",
            "pairing": pairing,
            "restart_needed": pending["restart"],
            "all_apps": [{"key": a.key, "name": a.name} for a in APPS],
            "tv_apk": settings.tv_apk_path().exists(),
            "backup_dir": cfg["backup_dir"],
            "backups": backups,
            "backups_error": why,  # a drive that does not answer: said here, never waited on
            "recs_avoid_genres": cfg["recs_avoid_genres"],
            "recs_languages": cfg["recs_languages"],
        }

    @change.get("/api/settings/checks")  # probes the TV, Kodi and TMDB: not for any Wi-Fi device
    def live():
        return checks.run(config, transport, vault, catalogs)

    def saved() -> dict:
        pending["restart"] = True
        return {"restart_needed": True}

    @change.put("/api/settings/jellyfin")
    def set_jellyfin(body: JellyfinStep):
        save_jellyfin(body)
        return saved()

    @change.put("/api/settings/streaming")
    def set_streaming(body: StreamingStep):
        save_streaming(body)
        return saved()

    @change.put("/api/settings/tvs")
    def set_tvs(body: TvsSettings):
        save_tvs(body.tvs, body.kodi_url, body.kodi_password)
        return saved()

    @change.post("/api/settings/tvs/pair", status_code=202)
    def pair(body: PairBody):
        cfg = settings.load(config)
        host = body.host or settings.default_tv(cfg)
        if host and host not in [t["host"] for t in cfg["tvs"]]:
            raise HTTPException(404, "Save that TV first")
        start_pairing(host)
        return {}

    @change.put("/api/settings/phones")
    def set_phones(body: PhonesStep):
        save_phones(body)
        return saved()

    @change.put("/api/settings/recs")
    def set_recs(body: RecsRules):
        """Applies at once: recommendations read the rules on every ask."""
        unknown = set(body.languages) - set(LANGUAGES) - {"any"}
        if unknown:
            raise HTTPException(422, f"unknown languages: {', '.join(sorted(unknown))}")
        genres = sorted({g.strip().lower() for g in body.avoid_genres if g.strip()})
        settings.save({"recs_avoid_genres": genres, "recs_languages": sorted(set(body.languages) - {"any"})}, config)
        return {"restart_needed": pending["restart"]}

    # Off-site: the backup folder to the Mac's shared off-site storage (jobkit.offsite: set up once per Mac, an
    # owner-only rclone config read only by the nightly backups), in this app's own folder there.
    def rclone_cmd(cfg: dict) -> str:
        found = shutil.which(
            cfg["rclone"], path=os.pathsep.join([os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
        )
        if not found:
            raise HTTPException(409, "rclone is not installed on this Mac: brew install rclone")
        return found

    def offsite_status(cfg: dict) -> dict:
        return shared_offsite.status(shared_offsite.CONFIG) | {"on": bool(cfg["offsite"])}

    def offsite_on(on: bool) -> None:
        cfg = settings.load(config)
        if not on:
            settings.save({"offsite": ""}, config)
            return
        if not shared_offsite.status(shared_offsite.CONFIG)["configured"]:
            raise HTTPException(409, "Set up the off-site storage first")
        if not cfg["backup_dir"]:
            raise HTTPException(409, "Pick the backup folder first: the off-site copy is of that folder")
        settings.save(
            {"offsite": OFFSITE_TO, "rclone_config": str(shared_offsite.CONFIG), "rclone": rclone_cmd(cfg)}, config
        )

    @change.put("/api/settings/tailnet")
    def set_tailnet(body: Tailnet):
        """Applies at once (Tailscale keeps it across restarts). Turning it off from a phone that came this way
        cuts that phone off: the page says so first."""
        return tailnet_on(body.on)

    @change.get("/api/settings/tailnet/qr.svg")
    def tailnet_qr():
        """The phone link as a QR code, made here (nothing is sent anywhere)."""
        url = tailnet_status(settings.load(config))["url"]
        if not url:
            raise HTTPException(409, "Tailscale is not signed in on this Mac")
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=2)
        return Response(img.to_string(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @change.post("/api/settings/offsite/key")
    def offsite_key(body: OffsiteKey):
        """Checks the key against the bucket, writes the Mac's off-site config, and returns its recovery code once."""
        try:
            code = shared_offsite.setup_b2(
                body.key_id, body.key, body.bucket, shared_offsite.CONFIG, rclone_cmd(settings.load(config))
            )
        except shared_offsite.SetupError as e:
            raise HTTPException(400, str(e)) from e
        return {"recovery_code": code}

    @change.put("/api/settings/offsite")
    def set_offsite(body: Offsite):
        """Applies at once: the nightly backup reads it each night."""
        offsite_on(body.on)
        return offsite_status(settings.load(config))

    @change.post("/api/setup/backup")
    def backup_step(body: BackupStep):
        open_only()
        folder = Path(body.dir.strip()).expanduser()
        if not folder.is_dir():
            raise HTTPException(422, "No such folder on this Mac (is the drive connected?)")
        settings.save({"backup_dir": str(folder)}, config)
        if body.offsite:
            offsite_on(True)
        progress.mark("backup", "done")
        return {}

    @change.put("/api/settings/backup")
    def set_backup(body: BackupDir):
        """Applies at once: the nightly job reads the folder each night (empty turns backups off)."""
        folder = body.dir.strip()
        if folder and not Path(folder).expanduser().is_dir():
            raise HTTPException(422, "No such folder on this Mac (is the drive connected?)")
        settings.save({"backup_dir": str(Path(folder).expanduser()) if folder else ""}, config)
        return {"restart_needed": pending["restart"]}

    @change.post("/api/settings/restart")
    def restart_now():
        restart()
        return {"restarting": True}

    api.include_router(change)
    return api
