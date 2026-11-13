"""Wakes the TV and opens the Jellyfin TV app over ADB, so 📺 TV works when the app is closed or the TV is asleep;
opens the streaming apps (Netflix, Peacock …) the same way.

The Air has its own ADB key (~/.config/mimir-flix/adbkey, owner-only): anyone who can read it gets a shell on the
TV, so it never leaves the Air. The TV trusts it once, through its "Allow USB debugging?" prompt (Pair on the Settings page).
"""

import os
import re
import shlex
from pathlib import Path

from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.exceptions import AdbConnectionError, AdbTimeoutError, DeviceAuthError, TcpTimeoutException


JELLYFIN_APP = "org.jellyfin.androidtv"
WHOLPHIN = "com.github.damontecres.wholphin"  # a Jellyfin TV app; used instead of Jellyfin's own when installed


NETFLIX = "com.netflix.ninja"


class TvError(Exception):
    pass


class Tv:
    def __init__(self, host: str, key: Path, port: int = 5555):
        self.host, self.port, self.key = host, port, key

    def _signer(self) -> PythonRSASigner:
        if not self.key.exists():
            self.key.parent.mkdir(parents=True, exist_ok=True)
            old = os.umask(0o077)  # private from the moment it exists
            try:
                keygen(str(self.key))
            finally:
                os.umask(old)
        os.chmod(self.key, 0o600)
        return PythonRSASigner(Path(f"{self.key}.pub").read_text(), self.key.read_text())

    def shell(self, command: str, auth_timeout: float = 10) -> str:
        dev = AdbDeviceTcp(self.host, self.port, default_transport_timeout_s=10)
        try:
            dev.connect(rsa_keys=[self._signer()], auth_timeout_s=auth_timeout)
            return dev.shell(command) or ""
        except DeviceAuthError as e:
            raise TvError("The TV does not trust this Mac yet: Pair it on the Settings page") from e
        except (AdbConnectionError, AdbTimeoutError, TcpTimeoutException, OSError) as e:
            raise TvError("The TV is not answering (is it plugged in and on the network?)") from e
        finally:
            dev.close()

    def open_jellyfin(self) -> bool:
        """Brings the Jellyfin TV app (Wholphin when installed, else Jellyfin's own) to the front, waking the TV.
        False when one was already in front: a play command then reaches it. An app left in the background keeps
        its Jellyfin session for minutes but cannot come forward by itself, so a play sent to it shows nothing."""
        apps = f"{WHOLPHIN}|{JELLYFIN_APP}"
        launch = "monkey -p {} -c android.intent.category.LEANBACK_LAUNCHER 1 >/dev/null 2>&1"
        out = self.shell(
            f"if [ \"$(dumpsys power | grep -c 'mWakefulness=Awake')\" = 1 ] &&"
            f" dumpsys activity activities | grep -m1 mResumedActivity | grep -qE '{apps}'; then echo front;"
            f" else input keyevent KEYCODE_WAKEUP; if pm path {WHOLPHIN} >/dev/null 2>&1;"
            f" then {launch.format(WHOLPHIN)}; else {launch.format(JELLYFIN_APP)}; fi; echo opened; fi"
        )
        return "front" not in out

    def play_in_wholphin(self, item_id: str, position_ms: int = 0) -> bool:
        """Plays a Jellyfin item in Wholphin through its playback intent (Wholphin ignores Jellyfin's remote play
        command), waking the TV. False when Wholphin is not installed: Jellyfin's own app is used then."""
        if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", item_id):  # it goes into a shell command
            raise TvError("not a Jellyfin item id")
        start = f"am start -a {WHOLPHIN}.PLAYBACK -n {WHOLPHIN}/.MainActivity --es itemId {item_id}"
        if position_ms > 0:
            start += f" --el position {int(position_ms)}"
        out = self.shell(
            f"if pm path {WHOLPHIN} >/dev/null 2>&1; then input keyevent KEYCODE_WAKEUP; {start} >/dev/null 2>&1;"
            " echo played; else echo none; fi"
        )
        return "played" in out

    def search_in_wholphin(self, query: str) -> bool:
        """Opens Wholphin's search on the TV with this text (its search intent), waking the TV. False when Wholphin
        is not installed. The text is shell-quoted."""
        start = f"am start -a android.intent.action.SEARCH -n {WHOLPHIN}/.MainActivity --es query {shlex.quote(query)}"
        out = self.shell(
            f"if pm path {WHOLPHIN} >/dev/null 2>&1; then input keyevent KEYCODE_WAKEUP; {start} >/dev/null 2>&1;"
            " echo searched; else echo none; fi"
        )
        return "searched" in out

    def open_overlay(self) -> None:
        """Starts the TV app's message overlay (I4), which stops by itself after a quiet spell. No wake key: a TV
        that is off stays off."""
        self.shell("am start-foreground-service -n dev.mimirflix.tv/.OverlayService")

    def now(self) -> dict:
        """What the TV shows: whether its screen is on, and the package of the app in front."""
        out = self.shell(
            "dumpsys power | grep -m1 mWakefulness=; dumpsys activity activities | grep -m1 mResumedActivity"
        )
        app = re.search(r"mResumedActivity: ActivityRecord\{\S+ u\d+ ([\w.]+)/", out)
        return {"screen_on": "mWakefulness=Awake" in out, "app": app.group(1) if app else ""}

    def open_app(self, package: str, title: str = "", url: str = "") -> None:
        """Opens a streaming app on the TV: at the title's own link when there is one (the app's https link, e.g.
        netflix.com/title/…), else at the app's search for the title when it takes Android's search intent, else at
        its home screen. The package comes from streaming.APPS and the link is checked to be the app's own; both are
        shell-quoted all the same. package may list the app's packages, "|"-joined: the installed one opens."""
        if "|" in package:
            names = package.split("|")
            out = self.shell(
                "; ".join(f"pm path {shlex.quote(p)} >/dev/null 2>&1 && echo {shlex.quote(p)}" for p in names)
            )
            package = (out.split() or names)[0]
        launch = f"monkey -p {package} -c android.intent.category.LEANBACK_LAUNCHER 1"
        search = f"am start -a android.intent.action.SEARCH -p {package} --es query {shlex.quote(title)}"
        view = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)} -p {package}"
        if package == NETFLIX:
            # Netflix drops a link while it runs (only brought to the front) and loses it behind "Who's watching?"
            # after a force-stop. Its own Android TV form works: its screens start over on the link (the app and its
            # profile stay), with its partner source.
            view = (
                f"am start -c android.intent.category.LEANBACK_LAUNCHER -a android.intent.action.VIEW "
                f"-d {shlex.quote(url)} -f 0x10808000 -e source 30 -n {NETFLIX}/.MainActivity"
            )
        if url:
            then = f"({search} 2>&1 | grep -q Error && {launch})" if title else launch
            self.shell(f"input keyevent KEYCODE_WAKEUP; {view} 2>&1 | grep -q Error && {then}")
        else:
            self.shell(f"input keyevent KEYCODE_WAKEUP; {search} 2>&1 | grep -q Error && {launch}" if title else launch)
