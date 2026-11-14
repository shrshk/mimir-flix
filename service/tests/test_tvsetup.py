"""Another TV set up like the first (tvsetup.py): over ADB at its shell, and Quick Connect at Jellyfin's edge."""

import httpx
import pytest

from mimir_tv import tvsetup
from mimir_tv.jellyfin import Jellyfin, JellyfinError
from mimir_tv.tv import Tv


class FakeTv(Tv):
    def __init__(self, apps):
        self.apps, self.sent, self.saver = set(apps), [], "com.other/.Saver"

    def shell(self, command, auth_timeout=10):
        self.sent.append(command)
        if command.startswith("pm path "):
            pkg = command.split()[2]
            return f"package:/data/app/{pkg}/base.apk" if pkg in self.apps else ""
        if command.startswith("settings get secure screensaver_components"):
            return self.saver + "\n"
        if command.startswith("settings put secure screensaver_components"):
            self.saver = command.split()[4].rstrip(";")
        return ""


def test_a_new_tv_gets_its_missing_apps_store_page_one_at_a_time_and_immich_as_screensaver():
    tv = FakeTv(apps=[])
    lines = tvsetup.setup(tv)
    assert "Wholphin: missing" in lines and "Immich TV: missing" in lines
    stores = [c for c in tv.sent if "market://" in c]
    assert stores == [
        "input keyevent KEYCODE_WAKEUP; am start -a android.intent.action.VIEW"
        " -d market://details?id=com.github.damontecres.wholphin"
    ]  # one page on the screen at a time
    assert tv.saver == "com.other/.Saver"  # no screensaver change until Immich TV is there

    tv = FakeTv(apps=[tvsetup.WHOLPHIN, tvsetup.IMMICH_TV])
    lines = tvsetup.setup(tv)
    assert not any("market://" in c for c in tv.sent)
    assert tv.saver == tvsetup.SCREENSAVER and "Screensaver: set to Immich TV" in lines
    assert "Screensaver: Immich TV" in tvsetup.setup(tv)  # already set: left alone


def test_wholphin_signs_in_through_quick_connect_with_the_code_it_shows():
    approved = []

    def jellyfin(request):
        if request.url.path == "/Users":
            return httpx.Response(200, json=[{"Id": "u1", "Policy": {"IsAdministrator": True}}])
        if request.url.path == "/QuickConnect/Authorize":
            approved.append((request.url.params["code"], request.url.params["userId"]))
            return httpx.Response(200, json=request.url.params["code"] == "123456")
        return httpx.Response(404)

    jf = Jellyfin("http://jf", "k", transport=httpx.MockTransport(jellyfin))
    tvsetup.approve(jf, "123456")
    assert approved == [("123456", "u1")]
    with pytest.raises(JellyfinError):
        tvsetup.approve(jf, "654321")  # an old or mistyped code
    with pytest.raises(ValueError):
        tvsetup.approve(jf, "12; reboot")  # only the 6 digits go anywhere
    assert len(approved) == 2


def test_a_tv_is_found_by_its_name_or_address_from_settings():
    cfg = {"tvs": [{"name": "Bedroom", "host": "192.0.2.40"}]}
    assert tvsetup.find_tv(cfg, "Bedroom") == tvsetup.find_tv(cfg, "192.0.2.40") == "192.0.2.40"
    with pytest.raises(SystemExit):
        tvsetup.find_tv(cfg, "Kitchen")
