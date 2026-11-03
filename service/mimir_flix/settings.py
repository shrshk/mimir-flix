"""Everything mimir-flix needs to know about this machine, in one file: ~/.config/mimir-flix/settings.json
(MIMIR_FLIX_CONFIG moves the folder; the Jellyfin API key and the TV's ADB key live there too).

Every setting is optional; MIMIR_FLIX_<NAME> in the environment overrides the file. See settings.example.json and
the README. The Setup and Settings pages write this file and show what works.
"""

import json
import os
from pathlib import Path

DEFAULTS = {
    "host": "127.0.0.1",  # 0.0.0.0 to reach mimir-flix from other devices without an https front door
    "port": 8723,
    # Phones reach mimir-flix at https://<this Mac's Tailscale name>:<tailnet_port> (tailscale serve, tailnet only),
    # turned on from Setup or Settings. A port of its own: other apps may serve theirs on the same name.
    "tailnet_port": 8726,
    "jellyfin_url": "http://127.0.0.1:8096",
    "media_root": "~/Documents/media",  # providers' .strm files: <id>/, <id>-movies/, <id>-live/
    # Names this app is reached by besides addresses, .local and tailnet names, e.g. the one Caddy serves it under
    # ("flix.home.example.com"); the resolver's name counts too. Any other name is refused (DNS rebinding).
    "hosts": [],
    "resolver": "",  # where players reach the resolver (Jellyfin's .strm files); http://127.0.0.1:<port> if empty
    "resolver_port": 8724,
    # The TVs: [{"name": "Living room", "host": "192.168.1.20", "kind": "android" | "fire"}], the first is the default
    # (📺 TV opens Jellyfin there over ADB; pair each once on the Settings page). tv_host is the older one-TV form,
    # read as the first TV.
    "tvs": [],
    "tv_host": "",
    "kodi_url": "",  # e.g. http://192.168.1.20:8080 (Kodi on the TV), with its password in kodi_password_file
    "kodi_password_file": "",  # "vault:kodi" (Settings saves it there) or an owner-only file
    # Where titles stream (TMDB, its token in tmdb-token beside the Jellyfin key) is filtered to the apps the house
    # pays for.
    "streaming_apps": "",  # e.g. "netflix,prime,peacock" (keys in streaming.APPS); empty: every app
    "streaming_region": "US",  # the country whose catalogues count
    # Recommendations everywhere (the page and an assistant) leave these out: genre names, and keep only these
    # original languages (codes like te, hi, en; empty: every language).
    # Providers (Settings adds them): [{"id", "name", "kind", …}]. kind is m3u or xtream (IPTV, with "server"?,
    # "tuner_id"?, "listings_id"?) or an installed plugin's kind (catalog.py). Their logins are in the vault, never here.
    "providers": [],
    # Nightly backup (D9 step 6) of mimir-flix's and Jellyfin's settings, lists and watch history: the folder (empty: off),
    # and where this Mac's Jellyfin keeps its data.
    "backup_dir": "",
    "jellyfin_data": "~/Library/Application Support/jellyfin",
    # After each clean backup, the backup folder off-site with rclone (jobkit's backup job): an rclone remote path,
    # e.g. "backup:mimir-flix" (a crypt remote over B2, S3, …; empty: off), rclone's config file (empty: rclone's
    # own), and the rclone command (a full path when launchd's PATH lacks it).
    "offsite": "",
    "rclone_config": "",
    "rclone": "rclone",
    "recs_avoid_genres": [],
    "recs_languages": [],
}


def config_dir() -> Path:
    return Path(os.environ.get("MIMIR_FLIX_CONFIG", Path.home() / ".config/mimir-flix"))


def tv_apk_path() -> Path:
    """Where the TV app's APK is kept for /tv/app.apk (repo mimir-flix-tv: its build is copied there)."""
    data = Path(os.environ.get("MIMIR_FLIX_DATA", Path.home() / "Library/Application Support/mimir-flix"))
    return data / "tv" / "mimir-flix-tv.apk"


def load(config: Path | None = None) -> dict:
    config = config or config_dir()
    path = config / "settings.json"
    found = json.loads(path.read_text()) if path.exists() else {}
    unknown = set(found) - set(DEFAULTS)
    if unknown:
        raise SystemExit(f"{path}: unknown settings {sorted(unknown)} (see settings.example.json)")
    out = DEFAULTS | found
    for name, default in DEFAULTS.items():
        env = os.environ.get(f"MIMIR_FLIX_{name.upper()}")
        if env is not None and not isinstance(default, list):
            out[name] = type(default)(env)
    if not out["tvs"] and out["tv_host"]:
        out["tvs"] = [{"name": "TV", "host": out["tv_host"], "kind": "android"}]
    for name in ("media_root", "kodi_password_file", "backup_dir", "jellyfin_data", "rclone_config"):
        out[name] = str(Path(out[name]).expanduser()) if out[name] and not out[name].startswith("vault:") else out[name]
    return out


def recs_rules(config: Path | None = None) -> dict:
    """The house's recommendation rules, read fresh (a change in Settings applies at once)."""
    cfg = load(config)
    return {"avoid_genres": cfg["recs_avoid_genres"], "languages": cfg["recs_languages"]}


def default_tv(cfg: dict) -> str:
    """The default TV's address (the first one), or "" when there is none."""
    return cfg["tvs"][0]["host"] if cfg["tvs"] else ""


def save(changes: dict, config: Path | None = None) -> Path:
    """Merges changes into settings.json (only keys that differ from the defaults are kept)."""
    config = config or config_dir()
    config.mkdir(parents=True, exist_ok=True)
    path = config / "settings.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    merged = {k: v for k, v in (current | changes).items() if DEFAULTS.get(k) != v}
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    return path


def kodi_password(spec: str, vault=None) -> str:
    """Kodi's web password: from the vault ("vault:kodi") or an owner-only file; "" when there is none."""
    if spec.startswith("vault:"):
        found = vault.get(spec.removeprefix("vault:")) if vault else None
        return (found or {}).get("password", "")
    path = Path(spec) if spec else None
    return path.read_text().strip() if path and path.exists() else ""
