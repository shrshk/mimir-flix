"""My list: shows and videos saved to watch later: a provider's shows and videos (catalog.py) and titles on the
streaming apps (by their TMDB id; an assistant can add several the person chose at once). Titles already in the home
library are Jellyfin favourites instead (jf_routes.py; entries of kind "jellyfin" here are from before, moved there
once). A pointer, never the media: nothing downloads and nothing goes into Jellyfin (adding to Jellyfin stays its own
action). One list for the house, in mimir-tv's own database. The hourly check counts each listed show's episodes; ones that appeared since are "new"
until the show is opened."""

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mimir_tv.recs import RecsError
from mimir_tv.catalog import Catalog, CatalogError
from mimir_tv.streaming import StreamingError

SCHEMA = """CREATE TABLE IF NOT EXISTS watchlist (
    url TEXT PRIMARY KEY,   -- a provider's show page, a video's link, or tmdb:movie/123 for a streaming title
    kind TEXT NOT NULL,     -- show | video | title
    title TEXT NOT NULL,
    thumb TEXT NOT NULL DEFAULT '',
    sub TEXT NOT NULL DEFAULT '',
    page TEXT NOT NULL DEFAULT '',  -- the list a video is in (a live channel's link is refreshed from there)
    here INTEGER NOT NULL DEFAULT 0,  -- a video the browser can play
    added REAL NOT NULL,
    episodes TEXT,          -- a show's episode links at the last check (JSON); NULL until the first one
    new INTEGER NOT NULL DEFAULT 0,
    item TEXT,              -- a streaming title as the page shows it (JSON); NULL for a provider's entries
    provider TEXT NOT NULL DEFAULT ''  -- the provider of a show or video; '' (saved before ids): the first one
)"""
# Jellyfin keeps no date for a favourite, so My list notes when it first saw each one ('' marks the first look).
FAVOURITES = "CREATE TABLE IF NOT EXISTS favourites (id TEXT PRIMARY KEY, added REAL NOT NULL)"


def episodes(cat: Catalog, show_url: str, ttl: int) -> set[str]:
    """Every episode link of a show: its videos and those of its season folders."""
    items = cat.page(show_url, ttl=ttl)
    found = {i["url"] for i in items if i["kind"] == "video"}
    for season in (i for i in items if i["kind"] == "folder"):
        found |= {i["url"] for i in cat.page(season["url"], ttl=ttl) if i["kind"] == "video"}
    return found


class Watchlist:
    def __init__(self, db: Path):
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute(SCHEMA)
            self.db.execute(FAVOURITES)
            have = {r[1] for r in self.db.execute("PRAGMA table_info(watchlist)")}  # a list from before
            if "item" not in have:
                self.db.execute("ALTER TABLE watchlist ADD COLUMN item TEXT")
            if "provider" not in have:
                self.db.execute("ALTER TABLE watchlist ADD COLUMN provider TEXT NOT NULL DEFAULT ''")

    def items(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM watchlist ORDER BY added DESC").fetchall()
        return [dict(r) for r in rows]

    def add(
        self,
        url: str,
        kind: str,
        title: str,
        thumb: str = "",
        sub: str = "",
        page: str = "",
        here: bool = False,
        provider: str = "",
    ) -> None:
        """Adding again keeps what the list knows (when it was added, the episodes seen)."""
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO watchlist (url, kind, title, thumb, sub, page, here, added, provider)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (url) DO NOTHING",
                (url, kind, title, thumb, sub, page, here, time.time(), provider),
            )

    def add_title(self, item: dict) -> bool:
        """A streaming title (TMDB), as the page shows it; True when it was not on the list yet."""
        return self.add_item(f"tmdb:{item['tmdb']}", "title", item, item["year"])

    def add_item(self, key: str, kind: str, item: dict, sub: str = "") -> bool:
        """A title kept as the page shows it (kind title: a streaming title)."""
        with self.lock, self.db:
            return (
                self.db.execute(
                    "INSERT INTO watchlist (url, kind, title, thumb, sub, added, item) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (url) DO NOTHING",
                    (key, kind, item["title"], item.get("thumb", ""), sub, time.time(), json.dumps(item)),
                ).rowcount
                > 0
            )

    def titles(self, kind: str = "title") -> list[dict]:
        return [json.loads(i["item"]) | {"added": i["added"]} for i in self.items() if i["kind"] == kind and i["item"]]

    def favourites_added(self, ids: dict[str, float]) -> dict[str, float]:
        """When each Jellyfin favourite went on My list. One made a favourite here is noted then (favourite()); one
        made in a Jellyfin app, when the list first sees it. ids: each favourite and the date to use if the list has
        never looked before (when it reached the library: the best guess for favourites made before this).
        Favourites since removed are forgotten (made again later, they count from then)."""
        with self.lock, self.db:
            known = dict(self.db.execute("SELECT id, added FROM favourites").fetchall())
            first, now = "" not in known, time.time()
            out = {i: known.get(i, ids[i] if first else now) for i in ids}
            self.db.execute("DELETE FROM favourites")
            self.db.executemany("INSERT INTO favourites VALUES (?, ?)", [("", 0), *out.items()])
        return out

    def favourite(self, item_id: str, on: bool) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM favourites WHERE id = ?", (item_id,))
            if on:
                self.db.execute("INSERT INTO favourites VALUES (?, ?)", (item_id, time.time()))

    def remove(self, url: str) -> bool:
        with self.lock, self.db:
            return self.db.execute("DELETE FROM watchlist WHERE url = ?", (url,)).rowcount > 0

    def seen(self, url: str) -> None:
        """The show was opened: its new episodes are no longer new."""
        with self.lock, self.db:
            self.db.execute("UPDATE watchlist SET new = 0 WHERE url = ? AND new > 0", (url,))

    def check(self, catalogs, ttl: int = 3000, only: str = "") -> int:
        """Counts new episodes of every listed show (or only that one); returns how many appeared this time. The
        first check of a show only notes what it has. A show its provider fails to list (or a provider since
        removed) is skipped until the next round. catalogs: catalog.Catalogs."""
        found = 0
        for show in [i for i in self.items() if i["kind"] == "show" and (not only or i["url"] == only)]:
            owner = catalogs.resolve(show["provider"])
            if owner is None:
                continue
            try:
                now = episodes(owner[1], show["url"], ttl)
            except CatalogError:
                continue
            before = set(json.loads(show["episodes"])) if show["episodes"] is not None else None
            fresh = len(now - before) if before is not None else 0
            found += fresh
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE watchlist SET episodes = ?, new = new + ? WHERE url = ?",
                    (json.dumps(sorted(now | (before or set()))), fresh, show["url"]),
                )
        return found


class Titles(BaseModel):
    tmdb: list[Annotated[str, Field(pattern=r"^(movie|tv)/\d+$")]] = Field(min_length=1, max_length=40)


def router(watchlist: Watchlist, details: Callable[[str], dict]) -> APIRouter:
    """My list's streaming titles (a provider's entries have their own routes, under /api/p/<provider id>/list)."""
    api = APIRouter(prefix="/api/list/titles")

    @api.get("")
    def titles():
        return watchlist.titles()

    @api.post("", status_code=201)
    def add(body: Titles):
        """Adds one or several titles (an assistant adds the ones the person chose in one go)."""

        def one(tmdb: str):
            try:
                d = details(tmdb)
            except (RecsError, StreamingError) as e:
                raise HTTPException(422, f"{tmdb}: {e}") from e
            keep = ("title", "year", "kind", "tmdb", "thumb", "backdrop", "desc", "apps", "genres", "rating")
            return {k: d[k] for k in keep}

        with ThreadPoolExecutor(6) as pool:
            items = list(pool.map(one, dict.fromkeys(body.tmdb)))
        added = [i["title"] for i in items if watchlist.add_title(i)]
        return {"added": added, "already": [i["title"] for i in items if i["title"] not in added]}

    @api.delete("", status_code=204)
    def remove(tmdb: str):
        if not watchlist.remove(f"tmdb:{tmdb}"):
            raise HTTPException(404, "not on My list")

    return api
