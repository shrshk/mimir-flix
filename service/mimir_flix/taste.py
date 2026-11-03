"""The house's taste, for an assistant that recommends (E2-2): mimir-flix holds the data and an assistant (an OpenClaw
skill) does the judging, so the app itself needs no model.

- What was watched (Jellyfin: movies and shows played, favourites; never home videos), 👍/👎 given on the page or in
  a chat, and the house's rules from Settings (genres to avoid, languages to keep).
- A wide pool of candidates: the recommendation categories plus titles like the best-liked ones, with what was
  watched, 👎 and anything against the rules already left out.
An assistant shows its picks in its own chat; mimir-flix is written to only when the person says so (My list, 👍/👎).
👍/👎 live in mimir-flix's own database. Nothing here is sent anywhere; the assistant reads it through MCP.
"""

import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mimir_flix.jellyfin import Jellyfin, JellyfinError
from mimir_flix.recs import Recs, RecsError
from mimir_flix.streaming import StreamingError

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS feedback (
        tmdb TEXT PRIMARY KEY,  -- movie/123 or tv/456
        title TEXT NOT NULL,
        vote INTEGER NOT NULL,  -- 1 👍, -1 👎
        at REAL NOT NULL
    )""",
]
POOL_CATEGORIES = ["buzzing", "worth_the_hype", "hidden_gems", "crowd_favourites", "new_on_your_apps"]
SEEDS = 5
MIN_VOTES = 200  # a pool for ranking leaves out thinly rated titles by default (Indian titles need a tenth)  # best-liked titles whose look-alikes join the pool


class FindTitle(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    year: int | None = Field(None, ge=1900, le=2100)
    kind: Literal["movie", "show"] = "movie"


class Finds(BaseModel):
    titles: list[FindTitle] = Field(min_length=1, max_length=30)


class Vote(BaseModel):
    tmdb: str = Field(pattern=r"^(movie|tv)/\d+$")
    vote: Literal["up", "down", "clear"]
    title: str = Field("", max_length=200)


class Taste:
    def __init__(self, db: Path, recs: Recs, jf: Jellyfin | None, history_ttl: int = 600):
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            for statement in SCHEMA:
                self.db.execute(statement)
        self.recs, self.jf = recs, jf
        self.ttl = history_ttl
        self._played: tuple[float, list[dict]] = (0.0, [])

    # 👍/👎
    def vote(self, tmdb: str, vote: str, title: str = "") -> None:
        with self.lock, self.db:
            if vote == "clear":
                self.db.execute("DELETE FROM feedback WHERE tmdb = ?", (tmdb,))
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO feedback VALUES (?, ?, ?, ?)",
                    (tmdb, title, 1 if vote == "up" else -1, time.time()),
                )

    def votes(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT tmdb, title, vote FROM feedback ORDER BY at DESC").fetchall()
        return [{"tmdb": t, "title": n, "vote": "up" if v > 0 else "down"} for t, n, v in rows]

    def disliked(self) -> set[str]:
        return {v["tmdb"] for v in self.votes() if v["vote"] == "down"}

    # What was watched
    def played(self) -> list[dict]:
        """Jellyfin's played and favourite titles, kept a few minutes (one page visit asks more than once)."""
        if self.jf is None:
            return []
        at, found = self._played
        if at > time.time() - self.ttl:
            return found
        try:
            found = self.jf.played()
        except JellyfinError:
            return found  # Jellyfin not answering: the last answer, or nothing
        self._played = (time.time(), found)
        return found

    def history(self, limit: int = 30) -> list[dict]:
        return [
            {k: p[k] for k in ("title", "kind", "year", "tmdb", "genres", "episodes", "favourite", "last_played")}
            for p in self.played()[:limit]
        ]

    def liked(self) -> list[dict]:
        """Best signals first: 👍, favourites, then shows watched far (episodes) and recent movies; with a TMDB id."""
        ups = [{"title": v["title"], "tmdb": v["tmdb"], "why": "👍"} for v in self.votes() if v["vote"] == "up"]
        played = [p for p in self.played() if p["tmdb"]]
        favs = [{"title": p["title"], "tmdb": p["tmdb"], "why": "favourite"} for p in played if p["favourite"]]
        shows = sorted((p for p in played if p["kind"] == "show"), key=lambda p: p["episodes"], reverse=True)
        far = [{"title": p["title"], "tmdb": p["tmdb"], "why": f"{p['episodes']} episodes"} for p in shows[:5]]
        recent = [{"title": p["title"], "tmdb": p["tmdb"], "why": "watched"} for p in played if p["kind"] == "movie"]
        out, known = [], set(self.disliked())
        for one in ups + favs + far + recent[:5]:
            if one["tmdb"] not in known:
                known.add(one["tmdb"])
                out.append(one)
        return out

    def profile(self) -> dict:
        """The taste in short: genres and people by weight, what is liked and disliked, the rules."""
        genres, people = Counter(), Counter()
        for p in self.played():
            weight = (2 if p["favourite"] else 0) + (1 + min(p["episodes"], 10) * 0.2 if p["kind"] == "show" else 1)
            for g in p["genres"]:
                genres[g] += weight
            for n in p["directors"] + p["cast"][:3]:
                people[n] += weight
        return {
            "genres": [{"name": g, "weight": round(w, 1)} for g, w in genres.most_common(10)],
            "people": [{"name": n, "weight": round(w, 1)} for n, w in people.most_common(10) if w >= 2],
            "liked": self.liked()[:15],
            "disliked": [{"title": v["title"], "tmdb": v["tmdb"]} for v in self.votes() if v["vote"] == "down"][:15],
            "watched_count": len(self.played()),
            "rules": self.recs.rules(),
        }

    # The pool
    def candidates(
        self,
        apps: str = "",
        kind: str = "any",
        limit: int = 60,
        min_votes: int = MIN_VOTES,
        year_from: int | None = None,
        language: str = "any",
    ) -> list[dict]:
        """The categories and look-alikes of the best-liked titles, each title once with every reason it came up.
        min_votes and year_from leave out titles too thinly rated or too old (Indian titles need a tenth of the
        votes); language narrows the categories to one original language."""

        def category(c):
            found = self.recs.recommend(
                c, apps=apps, kind=kind, language=language, year_from=year_from, limit=12, min_votes=min_votes
            )
            return found["items"]

        jobs = [lambda c=c: category(c) for c in POOL_CATEGORIES]
        seeds = [
            s for s in self.liked() if kind == "any" or s["tmdb"].startswith("tv/" if kind == "show" else "movie/")
        ]
        if language == "any":  # look-alikes come in any language
            jobs += [
                lambda s=s: self.recs.similar(s["tmdb"], apps, 8, min_votes, year_from)["items"] for s in seeds[:SEEDS]
            ]

        def run(job):
            try:
                return job()
            except (RecsError, StreamingError):
                return []  # one list failing (a seed TMDB lacks) leaves the rest

        with ThreadPoolExecutor(4) as pool:
            lists = list(pool.map(run, jobs))
        merged: dict[str, dict] = {}
        for found in lists:
            for i in found:
                if i["tmdb"] in merged:
                    merged[i["tmdb"]]["reasons"].append(i["why"])
                else:
                    merged[i["tmdb"]] = {k: v for k, v in i.items() if k != "why"} | {"reasons": [i["why"]]}
        # Titles that came up for more reasons first, then by rating.
        ranked = sorted(merged.values(), key=lambda i: (len(i["reasons"]), i["rating"]), reverse=True)
        return ranked[:limit]

    def find(self, titles: list[dict]) -> dict:
        """Many titles at once by name and year (e.g. the ones a review list mentions): each as a recommendation
        item with where it plays, under the same rules; watched, 👎 and unknown ones are reported apart."""

        def one(t):
            kind = "movie" if t.get("kind", "movie") == "movie" else "tv"
            year_key = "primary_release_year" if kind == "movie" else "first_air_date_year"
            params = {"query": t["title"], "language": "en-US"} | ({year_key: str(t["year"])} if t.get("year") else {})
            try:
                hit = next(iter(self.recs.s._get(f"/search/{kind}", **params).get("results") or []), None)
            except StreamingError:
                hit = None
            return t, (hit | {"media_type": kind}) if hit else None

        with ThreadPoolExecutor(6) as pool:
            found = list(pool.map(one, titles))
        hits = [h for _, h in found if h]
        items = self.recs.finish(hits, lambda item, where: "", [], len(hits)) if hits else []
        kept = {i["tmdb"] for i in items}
        return {
            "items": [{k: v for k, v in i.items() if k != "why"} for i in items],
            "not_found": [t["title"] for t, h in found if not h],
            "left_out": [t["title"] for t, h in found if h and f"{h['media_type']}/{h['id']}" not in kept],
        }


def router(taste: Taste) -> APIRouter:
    api = APIRouter()

    def tmdb(fn, *args):
        try:
            return fn(*args)
        except RecsError as e:
            raise HTTPException(400, str(e)) from e
        except StreamingError as e:
            raise HTTPException(502, str(e)) from e

    @api.get("/api/taste")
    def profile():
        return taste.profile()

    @api.get("/api/taste/history")
    def history(limit: int = 30):
        return taste.history(max(1, min(limit, 200)))

    @api.get("/api/recs/candidates")
    def candidates(
        apps: str = "",
        kind: str = "any",
        limit: int = 60,
        min_votes: int = MIN_VOTES,
        year_from: int | None = None,
        language: str = "any",
    ):
        if kind not in ("any", "movie", "show"):
            raise HTTPException(400, "kind is any, movie or show")
        return tmdb(taste.candidates, apps, kind, max(1, min(limit, 100)), max(0, min_votes), year_from, language)

    @api.get("/api/recs/similar")
    def similar(tmdb_id: str, apps: str = "", limit: int = 12):
        return tmdb(taste.recs.similar, tmdb_id, apps, max(1, min(limit, 30)))

    @api.get("/api/recs/details")
    def details(tmdb_id: str):
        return tmdb(taste.recs.details, tmdb_id)

    @api.post("/api/recs/find")
    def find(body: Finds):
        return tmdb(taste.find, [t.model_dump() for t in body.titles])

    @api.get("/api/recs/feedback")
    def votes():
        return taste.votes()

    @api.post("/api/recs/feedback")
    def vote(body: Vote):
        title = body.title
        if not title and body.vote != "clear":  # an assistant may leave it out: the list should still say what it is
            try:
                title = taste.recs.details(body.tmdb)["title"]
            except (RecsError, StreamingError):
                title = ""
        taste.vote(body.tmdb, body.vote, title)
        return {}

    return api
