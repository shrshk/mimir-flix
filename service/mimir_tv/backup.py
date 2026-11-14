"""Nightly backup of what cannot be rebuilt (D9 step 6), not the media: mimir-tv's database (My list, 👍/👎, jobs,
the vault, still encrypted) and settings.json, and Jellyfin's database and settings (libraries, users, watch
progress, played state, Live TV). Artwork, caches, logs and transcodes are left out (Jellyfin fetches artwork
again), and so is every secret file (API keys, the TV's ADB key, tokens, the vault's key, which stays in the
Keychain): after a restore on a new Mac, sign in to Jellyfin and save the logins again on the Settings page.
Jellyfin's own database does hold its API keys and devices' sign-ins, so each archive is owner-only (600): keep the
folder on a disk only you read, and off-site only encrypted (an rclone crypt remote).

One archive a night (mimir-tv-YYYY-MM-DD.tar.gz) in the folder Settings names, the newest KEEP kept. Databases are
copied with SQLite's own backup, so a copy taken while the apps run is consistent. scripts/restore-mac.sh puts one
back.

Off-site (when Settings names an rclone remote): after a clean snapshot the folder is synced there with jobkit's
backup job, then checked, as its own run. A snapshot that failed is never synced.
"""

import os
import sqlite3
import tarfile
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as Late
from datetime import date
from pathlib import Path

from jobkit.backup import backup as sync_off_site

KEEP = 7
JOB = "backup"
OFFSITE = "offsite"
# Jellyfin's folders worth keeping (its data folder holds the databases; config and root its settings and libraries)
JELLYFIN_PARTS = ["data", "config", "root", "plugins/configurations"]
JELLYFIN_SKIP = {"metadata", "cache", "log", "transcodes", "subtitles", "attachments", "keyframes"}
SECRET_FILES = {"jellyfin-api-key", "adbkey", "adbkey.pub", "mcp-token", "tmdb-token", "kodi-password", "vault-key"}


class BackupError(Exception):
    pass


def off_site(folder: Path, to: str, rclone_config: str = "", rclone_cmd: str = "rclone") -> str:
    """The backup folder to the rclone remote `to`, then a check; the run's summary. Raises when either fails."""
    if rclone_config:
        os.environ["RCLONE_CONFIG"] = rclone_config  # rclone reads it; this process runs no other rclone
    code, summary = sync_off_site([str(folder)], to, rclone_cmd=rclone_cmd)
    if code:
        raise BackupError(summary)
    return summary


def copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source, sqlite3.connect(dst) as target:
        source.backup(target)


def back_up(data: Path, config: Path, jellyfin: Path | None, dest: Path, today: date | None = None) -> Path:
    if not dest.is_dir():
        raise BackupError(f"the backup folder is not reachable ({dest}): is the drive connected?")
    if why := listed_soon(dest)[1]:  # a folder that does not answer would hold the job for good
        raise BackupError(why)
    name = f"mimir-tv-{(today or date.today()).isoformat()}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        if (data / "mimir-tv.db").exists():
            copy_db(data / "mimir-tv.db", stage / "mimir-tv" / "mimir-tv.db")
        if (config / "settings.json").exists():
            (stage / "mimir-tv").mkdir(exist_ok=True)
            (stage / "mimir-tv" / "settings.json").write_bytes((config / "settings.json").read_bytes())
        for part in JELLYFIN_PARTS if jellyfin and jellyfin.is_dir() else []:
            root = jellyfin / part
            for f in sorted(root.rglob("*")) if root.is_dir() else []:
                rel = f.relative_to(jellyfin)
                if not f.is_file() or JELLYFIN_SKIP & set(rel.parts) or f.name in SECRET_FILES:
                    continue
                if f.name.endswith(("-wal", "-shm", "-journal")):
                    continue  # the backup copy below already holds them
                target = stage / "jellyfin" / rel
                if f.suffix == ".db":
                    copy_db(f, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f.read_bytes())
        partial = dest / f".{name}.partial"
        partial.touch(mode=0o600)  # owner-only before anything is written: Jellyfin's database holds its sign-ins
        partial.chmod(0o600)
        with tarfile.open(partial, "w:gz") as tar:
            for top in ("mimir-tv", "jellyfin"):
                if (stage / top).exists():
                    tar.add(stage / top, arcname=top)
        out = dest / name
        partial.replace(out)
    for old in sorted(dest.glob("mimir-tv-*.tar.gz"), reverse=True)[KEEP:]:
        old.unlink()
    return out


STUCK = (
    "The backup folder is not answering. macOS may be asking whether mimir-tv may use files on that drive: allow it "
    "on this Mac (a prompt on its screen, or System Settings > Privacy & Security > Files and Folders), or check the "
    "drive."
)
_pending: dict[Path, Future] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(2, thread_name_prefix="backup-list")


def listed_soon(dest: Path, wait: float = 3) -> tuple[list[dict], str]:
    """The backups, or ([], why) when the folder cannot be read or does not answer within `wait` seconds (macOS holds a
    first look at an external drive until someone allows it; that wait never ends on its own). One look per folder at
    a time: a stuck drive holds one thread, not one per page load."""
    with _lock:
        look = _pending.get(dest)
        if look is None or look.done():
            look = _pending[dest] = _pool.submit(listed, dest)
    try:
        return look.result(timeout=wait), ""
    except Late:
        return [], STUCK
    except OSError as e:
        return [], f"The backup folder cannot be read: {e.strerror or e}"


def listed(dest: Path) -> list[dict]:
    if not dest.is_dir():
        return []
    return [{"name": f.name, "bytes": f.stat().st_size} for f in sorted(dest.glob("mimir-tv-*.tar.gz"), reverse=True)]
