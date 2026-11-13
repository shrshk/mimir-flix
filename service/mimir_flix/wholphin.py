"""Wholphin's home page, laid out like mimir-flix's: continue watching, one row per streaming app (On my apps),
then rows per language and kind (TV serials, web series, movies), TV channels, and new episodes.

Wholphin (an Android TV app for Jellyfin) reads its home rows from the user's Jellyfin display preferences
("default", client "Wholphin", custom pref "home_settings", version 1) when the TV has none of its own; this writes
them there. Settings ("wholphin_home" in settings.json): {"languages": ["Telugu", "Hindi", "English"],
"channels": ["…"]}. Rows use the tags mirror.py writes into each title's .nfo: the language, and
"<language> · <kind>"."""

import json

from mimir_flix.jellyfin import Jellyfin
from mimir_flix.onapps import LIBRARY, TRENDING
from mimir_flix.streaming import pick

PREFS_ID, CLIENT, KEY, VERSION = "default", "Wholphin", "home_settings", 1
KINDS = [("TV serial", "TV serials"), ("Web series", "web series")]


def rows(
    jf: Jellyfin,
    shows_lib: str,
    movies_lib: str,
    languages: list[str],
    channels: list[str],
    apps_lib: str | None = None,
    apps: list[str] = (),
) -> list[dict]:
    """The rows, in order; a row with nothing in it is left out. shows_lib, movies_lib, apps_lib: library item ids;
    apps: the streaming apps' names (onapps.py tags each title with them)."""

    def count(**filters) -> int:
        params = {"Recursive": "true", "Limit": "0", "userId": jf.user()} | filters
        return (jf._call("GET", "/Items", params) or {}).get("TotalRecordCount", 0)

    def query(name: str, parent: str, kind: str, **filters) -> dict:
        return {
            "type": "GetItems",
            "name": name,
            "filter": {
                "parentId": parent,
                "recursive": True,
                "includeItemTypes": [kind],
                "sortBy": ["DateCreated"],
                "sortOrder": ["Descending"],
            }
            | filters,
        }

    out = [{"type": "ContinueWatchingCombined"}]  # favourites (the watchlist) are in Wholphin's side menu
    lists = {
        p["Name"]: p["Id"]
        for p in (
            jf._call("GET", "/Items", {"IncludeItemTypes": "Playlist", "Recursive": "true", "userId": jf.user()}) or {}
        ).get("Items", [])
    }
    # mimir-flix's own rows for the apps, same names and order (onapps.playlists); a playlist row reads its order.
    for name in [TRENDING] + [f"Popular on {app}" for app in apps] if apps_lib else []:
        if name in lists:
            out.append({"type": "ByParent", "parentId": lists[name], "recursive": False})
    for lang in languages:
        shown = False
        for kind, label in KINDS:
            tag = f"{lang} · {kind}"
            if count(ParentId=shows_lib, IncludeItemTypes="Series", Tags=tag):
                out.append(query(f"{lang} {label}", shows_lib, "Series", tags=[tag]))
                shown = True
        if not shown and count(ParentId=shows_lib, IncludeItemTypes="Series", Tags=lang):
            out.append(query(f"{lang} shows", shows_lib, "Series", tags=[lang]))
        if count(ParentId=movies_lib, IncludeItemTypes="Movie", Tags=lang):
            out.append(query(f"{lang} movies", movies_lib, "Movie", tags=[lang]))
    for name in channels:
        found = (jf._call("GET", "/Studios", {"SearchTerm": name, "ParentId": shows_lib}) or {}).get("Items", [])
        studio = next((s for s in found if s["Name"].lower() == name.lower()), None)
        if studio:
            out.append(query(studio["Name"], shows_lib, "Series", studioIds=[studio["Id"]]))
    out.append({"type": "RecentlyAdded", "parentId": shows_lib})
    return out


def write(jf: Jellyfin, layout: list[dict]) -> bool:
    """Saves the rows to the user's Wholphin preferences, keeping the rest; False when they were already there."""
    params = {"userId": jf.user(), "client": CLIENT}
    prefs = jf._call("GET", f"/DisplayPreferences/{PREFS_ID}", params) or {}
    text = json.dumps({"rows": layout, "version": VERSION})
    custom = prefs.get("CustomPrefs") or {}
    if custom.get(KEY) == text:
        return False
    prefs["CustomPrefs"] = custom | {KEY: text}
    jf._call("POST", f"/DisplayPreferences/{PREFS_ID}", params, prefs)
    return True


def home(jf: Jellyfin, shows_name: str, movies_name: str, cfg: dict) -> str:
    """Writes the home page when the settings ask for one; a line for the jobs page."""
    want = cfg.get("wholphin_home") or {}
    if not want.get("languages"):
        return "no wholphin_home languages in Settings"
    libs = {x["Name"]: x.get("ItemId") for x in jf.libraries()}
    if not (libs.get(shows_name) and libs.get(movies_name)):
        return "the catalogue's libraries are not in Jellyfin yet"
    apps = [a.name for a in pick(cfg.get("streaming_apps", ""))]
    layout = rows(
        jf, libs[shows_name], libs[movies_name], want["languages"], want.get("channels", []), libs.get(LIBRARY), apps
    )
    return f"{len(layout)} rows" + (" written" if write(jf, layout) else ", unchanged")
