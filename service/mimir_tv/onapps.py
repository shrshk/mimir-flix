"""On my apps: what is on the house's streaming apps (streaming.py, from TMDB; the most popular few thousand each)
as titles in a Jellyfin library, so Jellyfin apps (Wholphin on the TV) list and search them with everything else.
Each is a .strm pointing at mimir-tv's /api/streaming/launch: played on the TV, it opens the streaming app at the
title, while the player shows a short black clip. Each title also gets a movie.nfo (plot, year, its apps as genres
and tags) and poster.jpg.

What was written is kept in .onapps.json ({folder: {"tmdb", "seen"}}); a title no longer listed for KEEP_FOR is
removed (only these generated files)."""

import json
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from mimir_tv import badges
from mimir_tv.mirror import folder_name, write_details
from mimir_tv.streaming import Streaming

TRENDING = "Trending on your apps"  # mimir-tv's home row names, kept for the playlists
LIBRARY = "On my apps"  # its Jellyfin library (movies), at <media_root>/apps
STATE = ".onapps.json"
KEEP_FOR = 14 * 24 * 3600
PER_APP = 3000  # each app's most popular titles (movies and shows together); TMDB lists fewer for most
POSTERS_PER_RUN = 3000  # poster downloads a run (from TMDB's image server); the rest come on later runs


def state(root: Path) -> dict[str, dict]:
    f = root / STATE
    return json.loads(f.read_text()) if f.exists() else {}


def launch_line(base: str, app: str, tmdb: str, title: str) -> str:
    return f"{base}/api/streaming/launch?app={quote(app)}&tmdb={quote(tmdb, safe='')}&title={quote(title, safe='')}\n"


def fill(
    streaming: Streaming, root: Path, base: str, http: httpx.Client | None = None, now: float | None = None
) -> dict:
    """One refresh: the apps' popular titles written or kept, those gone for KEEP_FOR removed. base: mimir-tv's
    address as the TV reaches it. Returns counts."""
    now = time.time() if now is None else now
    root.mkdir(parents=True, exist_ok=True)
    kept = state(root)
    taken = {name.lower(): s["tmdb"] for name, s in kept.items()}
    by_tmdb = {s["tmdb"]: name for name, s in kept.items()}
    out = {"titles": 0, "new": 0, "removed": 0, "posters": 0}
    budget = [POSTERS_PER_RUN]
    own = http is None
    client = http or httpx.Client(timeout=20, follow_redirects=True)
    try:
        for it in streaming.catalog(PER_APP):
            title = it["title"]
            label = f"{title} ({it['year']})" if it.get("year") else title
            name = by_tmdb.get(it["tmdb"]) or folder_name(label, it["tmdb"], taken)
            folder = root / name
            new = not folder.exists()
            folder.mkdir(exist_ok=True)
            app = it["apps"][0]
            strm, line = folder / f"{name}.strm", launch_line(base, app["app"], it["tmdb"], title)
            if not strm.exists() or strm.read_text() != line:
                strm.write_text(line)
            kinds = [a["name"] for a in it["apps"]] + ["Show" if it["kind"] == "show" else "Movie"]
            details = {"title": label, "plot": it.get("desc", ""), "thumb": it.get("thumb", ""), "genres": kinds}
            labels = badges.for_apps([a["name"] for a in it["apps"]])
            out["posters"] += write_details(folder, "movie", details, None, client, budget, labels=labels)[1]
            taken[name.lower()], by_tmdb[it["tmdb"]] = it["tmdb"], name
            kept[name] = {"tmdb": it["tmdb"], "seen": now}
            out["titles"] += 1
            out["new"] += new
    finally:
        if own:
            client.close()
    for name, s in list(kept.items()):
        if now - s["seen"] > KEEP_FOR:
            folder = root / name
            files = (f"{name}.strm", "movie.nfo", "poster.jpg", badges.ORIGINAL, badges.DRAWN)
            for f in (folder / n for n in files):
                f.unlink(missing_ok=True)
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
            del kept[name]
            out["removed"] += 1
    tmp = root / (STATE + ".tmp")
    tmp.write_text(json.dumps(kept, indent=1, sort_keys=True))
    tmp.replace(root / STATE)
    return out


def playlists(streaming: Streaming, jf, root: Path, per_row: int = 20) -> int:
    """mimir-tv's home rows for the apps, as Jellyfin playlists with the same names, titles and order
    ("Trending on your apps", "Popular on <app>"), so Jellyfin apps show the same lists (playlists keep their
    order; Wholphin's rows read them). Returns how many playlists changed."""
    lib = next((x for x in jf.libraries() if x["Name"] == LIBRARY), None)
    if not lib:
        return 0
    params = {"ParentId": lib["ItemId"], "Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "Path"}
    found = (jf._call("GET", "/Items", params | {"userId": jf.user()}) or {}).get("Items", [])
    tmdb_of = {name: s["tmdb"] for name, s in state(root).items()}
    item_of = {tmdb_of[Path(i["Path"]).parent.name]: i["Id"] for i in found if Path(i["Path"]).parent.name in tmdb_of}
    have = {
        p["Name"]: p["Id"]
        for p in (
            jf._call("GET", "/Items", {"IncludeItemTypes": "Playlist", "Recursive": "true", "userId": jf.user()}) or {}
        ).get("Items", [])
    }
    changed = 0
    wanted = [(TRENDING, streaming.trending())] + [
        (f"Popular on {r['name']}", r["items"]) for r in streaming.rows(per_row=per_row, at_least=1)
    ]
    for name, items in wanted:
        want = [item_of[i["tmdb"]] for i in items if i["tmdb"] in item_of]
        if not want:
            continue
        if name not in have:
            body = {"Name": name, "Ids": want, "UserId": jf.user(), "MediaType": "Video"}
            jf._call("POST", "/Playlists", None, body)
            changed += 1
            continue
        pid = have[name]
        now = (jf._call("GET", f"/Playlists/{pid}/Items", {"userId": jf.user()}) or {}).get("Items", [])
        if [i["Id"] for i in now] == want:
            continue
        if now:
            entries = ",".join(i["PlaylistItemId"] for i in now)
            jf._call("DELETE", f"/Playlists/{pid}/Items", {"EntryIds": entries})
        jf._call("POST", f"/Playlists/{pid}/Items", {"Ids": ",".join(want), "UserId": jf.user()})
        changed += 1
    return changed
