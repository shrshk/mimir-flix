"""Into Jellyfin (into.py): Live TV's channel groups follow the house's languages, at Jellyfin's HTTP edge."""

import json

import httpx

from mimir_tv import into
from mimir_tv.jellyfin import Jellyfin


def test_live_tv_asks_for_the_houses_languages_only_where_a_tuner_takes_groups():
    plugin = {
        "Id": "a",
        "Type": "m3u",
        "Url": "http://127.0.0.1:8723/get.php?username&password&groups=Telugu&output=ts",
    }
    other = {"Id": "b", "Type": "m3u", "Url": "http://iptv.example/list.m3u"}
    posted = []

    def handler(request):
        if request.url.path == "/System/Configuration/livetv":
            return httpx.Response(200, json={"TunerHosts": [plugin, other]})
        if request.url.path == "/LiveTv/TunerHosts" and request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"Id": posted[-1]["Id"]})
        return httpx.Response(404)

    jf = Jellyfin("http://jf", "k", transport=httpx.MockTransport(handler))
    assert into.set_live_groups(jf, ["Telugu", "Hindi", "English"]) == 1
    # Only the groups change (the rest of the address as it was, the same tuner updated by its id).
    want = "http://127.0.0.1:8723/get.php?username&password&groups=Telugu,Hindi,English&output=ts"
    assert posted == [plugin | {"Url": want}]
    assert into.set_live_groups(jf, []) == 0 and len(posted) == 1  # no languages: left as it is
