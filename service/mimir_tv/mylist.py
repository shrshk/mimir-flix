"""My list is the user's Jellyfin favourites: one list for every Jellyfin app (jf_routes.py). Jellyfin keeps no date
for a favourite, so mimir-tv notes when it first saw each one, to show the newest first. That is all it keeps.

Picks from an assistant that are not in the library stay with the assistant (mimir-claw keeps its own list)."""

import sqlite3
import threading
import time
from pathlib import Path

FAVOURITES = "CREATE TABLE IF NOT EXISTS favourites (id TEXT PRIMARY KEY, added REAL NOT NULL)"  # '' marks a look


class ListDates:
    def __init__(self, db: Path):
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute(FAVOURITES)
            self.db.execute("DROP TABLE IF EXISTS watchlist")  # My list's own titles, before it was favourites only

    def added(self, ids: dict[str, float]) -> dict[str, float]:
        """When each favourite went on My list. One made a favourite here is noted then (favourite()); one made in a
        Jellyfin app, when the list first sees it. ids: each favourite and the date to use if the list has never
        looked before (when it reached the library: the best guess for favourites made before this). Favourites
        since removed are forgotten (made again later, they count from then)."""
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
