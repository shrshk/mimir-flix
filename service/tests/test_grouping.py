"""The catalogue as collections per language, then kind (grouping.py), against Jellyfin at its HTTP edge."""

import json

import httpx

from mimir_tv import grouping
from mimir_tv.jellyfin import Jellyfin

SHOWS, MOVIES = "lib-shows", "lib-movies"


class FakeJellyfin:
    def __init__(self):
        self.titles = {  # id: (library, type, tags)
            "s1": (SHOWS, "Series", {"Telugu", "Telugu · TV serial"}),
            "s2": (SHOWS, "Series", {"Telugu", "Telugu · Web series"}),
            "s3": (SHOWS, "Series", {"English"}),
            "m1": (MOVIES, "Movie", {"Telugu"}),
            "m2": (MOVIES, "Movie", {"English"}),
        }
        self.boxsets: dict[str, dict] = {"user": {"Name": "Mine", "kids": {"m2"}, "sort": None}}
        self.calls = 0
        self.gone: set[str] = set()  # listed by a search a moment ago, removed by a scan since

    def handler(self, request: httpx.Request) -> httpx.Response:
        q, path, m = request.url.params, request.url.path, request.method
        ids = [i for i in q.get("Ids", "").split(",") if i]
        if path == "/Users":
            return httpx.Response(200, json=[{"Id": "u1", "Policy": {"IsAdministrator": True}}])
        if path == "/Items" and q.get("IncludeItemTypes") == "BoxSet":
            return httpx.Response(200, json={"Items": [{"Id": k, "Name": b["Name"]} for k, b in self.boxsets.items()]})
        if path == "/Items" and q["ParentId"] in self.boxsets:
            return httpx.Response(200, json={"Items": [{"Id": i} for i in self.boxsets[q["ParentId"]]["kids"]]})
        if path == "/Items":
            hits = [
                i
                for i, (lib, kind, tags) in self.titles.items()
                if lib == q["ParentId"] and kind == q["IncludeItemTypes"] and q["Tags"] in tags
            ]
            return httpx.Response(200, json={"Items": [{"Id": i} for i in hits]})
        if path == "/Collections" and m == "POST":
            cid = f"c{len(self.boxsets)}"
            if self.gone & set(ids):
                return httpx.Response(400, text="Error processing request.")
            self.boxsets[cid] = {"Name": q["Name"], "kids": set(ids), "sort": None}
            return httpx.Response(200, json={"Id": cid})
        if path.startswith("/Collections/") and path.endswith("/Items"):
            self.calls += 1
            if m == "POST" and self.gone & set(ids):
                return httpx.Response(400, text="Error processing request.")  # Jellyfin refuses the whole call
            kids = self.boxsets[path.split("/")[2]]["kids"]
            kids.update(ids) if m == "POST" else kids.difference_update(ids)
            return httpx.Response(204)
        if path.startswith("/Items/"):
            b = self.boxsets[path.split("/")[2]]
            if m == "POST":
                b["sort"] = json.loads(request.content)["ForcedSortName"]
                return httpx.Response(204)
            return httpx.Response(200, json={"Name": b["Name"], "ForcedSortName": b["sort"]})
        return httpx.Response(404)

    def named(self, name):
        return next(b for b in self.boxsets.values() if b["Name"] == name)


def test_the_catalogue_is_grouped_by_language_then_kind_in_the_houses_order():
    fake = FakeJellyfin()
    jf = Jellyfin("http://jf", "k", transport=httpx.MockTransport(fake.handler))
    assert grouping.group(jf, SHOWS, MOVIES, ["Telugu", "Hindi", "English"]).startswith("Telugu, English:")

    telugu = fake.named("Telugu")
    inner = {fake.boxsets[c]["Name"] for c in telugu["kids"]}
    assert inner == {"Telugu TV serials", "Telugu web series", "Telugu movies"}  # Hindi has nothing: no collection
    assert fake.named("Telugu TV serials")["kids"] == {"s1"} and fake.named("Telugu movies")["kids"] == {"m1"}
    assert fake.named("English shows")["kids"] == {"s3"}  # no kind: one collection of shows
    # Listed in the house's order, each kind under its language, not alphabetically.
    assert telugu["sort"] == "01 Telugu" and fake.named("English")["sort"] == "03 English"
    assert fake.named("Telugu TV serials")["sort"].startswith("01 Telugu ")
    assert fake.named("Mine")["kids"] == {"m2"} and fake.named("Mine")["sort"] is None  # the user's own: untouched

    # A title that changes kind moves; nothing else is called again.
    fake.titles["s1"] = (SHOWS, "Series", {"Telugu", "Telugu · Web series"})
    fake.titles["s2"] = (SHOWS, "Series", {"Telugu", "Telugu · TV serial"})
    before = fake.calls
    grouping.group(jf, SHOWS, MOVIES, ["Telugu", "Hindi", "English"])
    assert fake.named("Telugu TV serials")["kids"] == {"s2"} and fake.named("Telugu web series")["kids"] == {"s1"}
    assert fake.calls - before == 4  # one add and one take-out in each of the two


def test_a_title_gone_while_grouping_leaves_out_only_that_title():
    fake = FakeJellyfin()
    jf = Jellyfin("http://jf", "k", transport=httpx.MockTransport(fake.handler))
    fake.titles["s4"] = (SHOWS, "Series", {"Telugu", "Telugu · TV serial"})
    fake.gone.add("s4")
    grouping.group(jf, SHOWS, MOVIES, ["Telugu"])
    assert fake.named("Telugu TV serials")["kids"] == {"s1"}
