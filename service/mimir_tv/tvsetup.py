"""Another TV, set up like the first: the apps it needs, Immich as its screensaver, and Wholphin signed in.

Everything else follows the Jellyfin user, so it comes along by itself: Wholphin's home rows (wholphin.py), the
catalogue's collections (grouping.py), the apps' playlists (onapps.py), titles, posters and badges.

    mimir-tv-setup <tv>                  checks the TV and does what it can over ADB
    mimir-tv-setup <tv> --approve CODE   signs Wholphin in: the Quick Connect code it shows

<tv> is a TV's name or address from Settings (pair it there once first: the TV trusts this machine's ADB key).
"""

import argparse
import re
import sys

from mimir_tv import settings
from mimir_tv.jellyfin import Jellyfin, JellyfinError
from mimir_tv.tv import Tv, TvError

WHOLPHIN = "com.github.damontecres.wholphin"
IMMICH_TV = "nl.giejay.android.tv.immich"
APPS = [("Wholphin", WHOLPHIN), ("Immich TV", IMMICH_TV)]  # both on the Play Store
SCREENSAVER = f"{IMMICH_TV}/.screensaver.ScreenSaverService"


def installed(tv: Tv, package: str) -> bool:
    return package in tv.shell(f"pm path {package} 2>/dev/null")


def open_store(tv: Tv, package: str) -> None:
    """The app's Play Store page on the TV: the person presses Install (nothing is installed from elsewhere)."""
    tv.shell(f"input keyevent KEYCODE_WAKEUP; am start -a android.intent.action.VIEW -d market://details?id={package}")


def screensaver(tv: Tv) -> str:
    return tv.shell("settings get secure screensaver_components").strip()


def set_screensaver(tv: Tv) -> None:
    tv.shell(f"settings put secure screensaver_components {SCREENSAVER}; settings put secure screensaver_enabled 1")


def setup(tv: Tv) -> list[str]:
    """What is done and what is left, a line each. Opens the store page of the first missing app only (one page at
    a time on the screen); run again after installing it."""
    lines = []
    missing = [(name, pkg) for name, pkg in APPS if not installed(tv, pkg)]
    for name, pkg in APPS:
        lines.append(f"{name}: installed" if (name, pkg) not in missing else f"{name}: missing")
    if missing:
        name, pkg = missing[0]
        open_store(tv, pkg)
        lines.append(f"{name}'s Play Store page is open on the TV: press Install, then run this again")
    if ("Immich TV", IMMICH_TV) not in missing:
        if screensaver(tv) != SCREENSAVER:
            set_screensaver(tv)
            lines.append("Screensaver: set to Immich TV")
        else:
            lines.append("Screensaver: Immich TV")
    lines += [
        "Left to do on the TV:",
        "  1. Wholphin: pick this house's server, then Add user: it shows a Quick Connect code;"
        " run this again with --approve CODE",
        "  2. Wholphin: Settings > Home: use the server's layout (the rows this house set up)",
        "  3. Immich TV: sign in, then Settings > Screensaver: pick the albums it shows",
    ]
    return lines


def approve(jf: Jellyfin, code: str) -> None:
    """Signs a TV app in as the house's Jellyfin user through Quick Connect: no password typed on the TV."""
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("a Quick Connect code is 6 digits")
    if not jf._call("POST", "/QuickConnect/Authorize", {"code": code, "userId": jf.user()}):
        raise JellyfinError("Jellyfin did not accept that code (it lasts a few minutes: ask the TV for a new one)")


def find_tv(cfg: dict, which: str) -> str:
    for t in cfg["tvs"]:
        if which in (t.get("name"), t.get("host")):
            return t["host"]
    raise SystemExit(f"no TV called {which!r} in Settings (pair it there first)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mimir-tv-setup", description=__doc__.splitlines()[0])
    ap.add_argument("tv", help="the TV's name or address from Settings")
    ap.add_argument("--approve", metavar="CODE", help="the Quick Connect code Wholphin shows")
    args = ap.parse_args(argv)
    config = settings.config_dir()
    cfg = settings.load(config)
    if args.approve:
        key = config / "jellyfin-api-key"
        try:
            approve(Jellyfin(cfg["jellyfin_url"], key.read_text().strip() if key.exists() else ""), args.approve)
        except (ValueError, JellyfinError) as e:
            print(e, file=sys.stderr)
            return 1
        print("Wholphin is signed in")
        return 0
    try:
        for line in setup(Tv(find_tv(cfg, args.tv), config / "adbkey")):
            print(line)
    except TvError as e:
        print(f"{e}\n(pair the TV on the Settings page first)", file=sys.stderr)
        return 1
    return 0
