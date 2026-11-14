"""What works on this Mac and what to fix: the Settings page's live ✓/✗ (it replaced mimir-tv-check). Each check
is one part (Jellyfin, each provider, TMDB, each TV, Kodi) and they run side by side, so the slowest one sets the time.

ok is True (works), False (set up but broken: the fix says what to do) or None (an optional part not set up).
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from mimir_tv import settings
from mimir_tv.jellyfin import Jellyfin, JellyfinError
from mimir_tv.kodi import Kodi, KodiError
from mimir_tv.catalog import CatalogError, Catalogs
from mimir_tv.streaming import pick
from mimir_tv.tv import Tv, TvError


def result(card: str, name: str, ok: bool | None, detail: str = "", fix: str = "") -> dict:
    return {"card": card, "name": name, "ok": ok, "detail": detail, "fix": "" if ok else fix}


def jellyfin(cfg: dict, config: Path, transport: httpx.BaseTransport | None = None) -> dict:
    key_file = config / "jellyfin-api-key"
    if not key_file.exists():
        return result("jellyfin", "Jellyfin", False, "no API key yet", "Sign in below to make mimir-tv's key")
    try:
        found = Jellyfin(cfg["jellyfin_url"], key_file.read_text().strip(), transport).libraries()
        return result("jellyfin", "Jellyfin", True, f"{cfg['jellyfin_url']}, {len(found)} libraries")
    except JellyfinError:
        return result(
            "jellyfin",
            "Jellyfin",
            False,
            f"no answer, or the key is refused, at {cfg['jellyfin_url']}",
            "Open the Jellyfin app on this Mac; if it runs, sign in again below for a new key",
        )


def provider(entry: dict, catalogs: Catalogs) -> dict:
    """A plugin provider's login, tried once."""
    try:
        catalogs.make(entry).check()
        return result("providers", entry["name"], True, "the login works")
    except CatalogError as e:
        return result("providers", entry["name"], False, str(e), "Remove it and add it again with the right login")


def resolver(cfg: dict, any_plugin: bool) -> dict:
    address = cfg["resolver"] or f"http://127.0.0.1:{cfg['resolver_port']}"
    if not any_plugin:
        return result("phones", "Provider links", None, "only used with a provider plugin")
    try:
        ok = httpx.get(f"{address}/play", timeout=5).status_code == 404  # no link given: the resolver answers 404
    except httpx.HTTPError:
        ok = False
    return result("phones", "Provider links", ok, address, "Check the address below (and the https front door)")


def tmdb(cfg: dict, config: Path) -> dict:
    token_file = config / "tmdb-token"
    if not token_file.exists():
        return result("streaming", "Streaming apps", None, "not set up (optional)")
    apps = ", ".join(a.name for a in pick(cfg["streaming_apps"]))
    token = token_file.read_text().strip()
    jwt = token.startswith("eyJ")  # a Read Access Token; otherwise a v3 API key (TMDB takes it in the URL)
    try:
        r = httpx.get(
            "https://api.themoviedb.org/3/authentication",
            headers={"Authorization": f"Bearer {token}"} if jwt else {},
            params={} if jwt else {"api_key": token},
            timeout=10,
        )
        ok = r.status_code == 200
    except httpx.HTTPError:
        ok = False
    return result("streaming", "Streaming apps", ok, apps, "Paste a new TMDB API Read Access Token below")


def tv(config: Path, entry: dict) -> dict:
    name = f"{entry['name']} ({entry['host']})"
    try:
        Tv(entry["host"], config / "adbkey").shell("echo ok")
        return result("tvs", name, True, "trusts this Mac")
    except TvError as e:
        return result("tvs", name, False, str(e), "Turn the TV on, then Pair and accept the prompt on the TV")


def kodi(cfg: dict, vault=None) -> dict:
    if not (cfg["kodi_url"] and cfg["kodi_password_file"]):
        return result("tvs", "Kodi", None, "not set up (optional)")
    try:
        Kodi(cfg["kodi_url"], settings.kodi_password(cfg["kodi_password_file"], vault)).rpc("JSONRPC.Ping")
        return result("tvs", "Kodi", True, cfg["kodi_url"])
    except KodiError as e:
        if "not answering" in str(e):  # Kodi closed on the TV is its usual state: nothing to fix
            return result("tvs", "Kodi", None, f"{cfg['kodi_url']}, not open on the TV right now")
        return result("tvs", "Kodi", False, str(e), "Save Kodi's web password again below")
    except OSError:
        return result("tvs", "Kodi", False, "its password file cannot be read", "Save Kodi's web password below")


def run(
    config: Path, transport: httpx.BaseTransport | None = None, vault=None, catalogs: Catalogs | None = None
) -> list[dict]:
    cfg = settings.load(config)
    plugins = catalogs.entries() if catalogs else []
    checks: list[Callable[[], dict]] = [
        lambda: jellyfin(cfg, config, transport),
        *[lambda e=e: provider(e, catalogs) for e in plugins],
        lambda: resolver(cfg, bool(plugins)),
        lambda: tmdb(cfg, config),
        *[lambda e=e: tv(config, e) for e in cfg["tvs"]],
        lambda: kodi(cfg, vault),
    ]
    with ThreadPoolExecutor(len(checks)) as pool:
        return list(pool.map(lambda c: c(), checks))
