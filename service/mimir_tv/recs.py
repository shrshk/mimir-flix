"""Recommendations (E2): what to watch, by category, from TMDB's own data. Each category is one discover query per
kind (popularity, rating, vote count, dates); the person narrows it to their apps (or any apps, or some), a language,
a genre, movies or shows, a minimum rating and years, and TMDB filters on its side. What the house has already
watched in Jellyfin is left out. Every request is kept a day (Streaming's cache), so asking again is free.

The thresholds are starting points (TMDB has fewer votes than IMDb, and Indian titles far fewer than English ones);
tune them by what the house finds useful.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException

from mimir_tv.streaming import APPS, Streaming, StreamingError

INDIAN = {"hi", "te", "ta", "ml", "kn", "mr", "bn", "pa", "gu"}
LANGUAGES = {
    "any": "Any language",
    "en": "English",
    "hi": "Hindi",
    "te": "Telugu",
    "ta": "Tamil",
    "ml": "Malayalam",
    "kn": "Kannada",
    "mr": "Marathi",
    "bn": "Bengali",
    "ko": "Korean",
    "ja": "Japanese",
    "es": "Spanish",
}
LOOKUPS = 24  # results whose apps are looked up (to say where each plays); the rest are rarely needed

# A category: how TMDB sorts, how recent, the rating window and the vote floor (English-and-other, Indian).
CATEGORIES = {
    "buzzing": {"label": "Buzzing now", "sort": "popularity.desc", "days": 60, "votes": (20, 5)},
    "worth_the_hype": {
        "label": "Worth the hype",
        "sort": "popularity.desc",
        "days": 120,
        "min": 7.0,
        "votes": (300, 25),
    },
    "skip_the_hype": {"label": "Skip the hype", "sort": "popularity.desc", "days": 120, "max": 6.0, "votes": (150, 25)},
    "crowd_favourites": {"label": "Crowd favourites", "sort": "vote_average.desc", "min": 7.5, "votes": (2000, 150)},
    "hidden_gems": {
        "label": "Hidden gems",
        "sort": "vote_average.desc",
        "min": 7.3,
        "votes": (200, 15),
        "max_votes": (1500, 150),
    },
    "new_on_your_apps": {
        "label": "New on your apps",
        "sort": "popularity.desc",
        "days": 30,
        "min": 6.0,
        "votes": (10, 3),
    },
}


class RecsError(Exception):
    pass


def why(cat: str, item: dict, app_names: list[str]) -> str:
    """One line, from the data only, that the person can check."""
    r, v = item["rating"], item["votes"]
    score = f"{r:.1f}/10 from {v:,} votes" if v else "not rated yet"
    where = f" on {app_names[0]}" if app_names else ""
    return {
        "buzzing": f"One of the most watched new {item['kind']}s{where} right now; {score}.",
        "worth_the_hype": f"Popular right now and well rated: {score}.",
        "skip_the_hype": f"Popular right now but poorly rated: {score}.",
        "crowd_favourites": f"Loved by a lot of viewers: {score}.",
        "hidden_gems": f"Rated highly by the few who saw it: {score}.",
        "new_on_your_apps": f"New{where} ({item['date']}); {score}.",
    }[cat]


class Recs:
    def __init__(
        self,
        streaming: Streaming,
        seen: Callable[[], set[str]] | None = None,
        rules: Callable[[], dict] | None = None,
        disliked: Callable[[], set[str]] | None = None,
    ):
        self.s = streaming
        self.seen = seen or (lambda: set())  # titles the house has watched (lower case), from Jellyfin
        # The house's rules from Settings: {"avoid_genres": [names], "languages": [codes]} (empty: none)
        self.rules = rules or (lambda: {})
        self.disliked = disliked or (lambda: set())  # TMDB keys ("movie/123") given 👎

    def genre_names(self, kind: str) -> dict[int, str]:
        """TMDB's genre names by id, as TMDB writes them ("Science Fiction"), kept a day."""
        found = self.s._get(f"/genre/{kind}/list", language="en-US").get("genres", [])
        return {g["id"]: g["name"] for g in found}

    def allowed(self, h: dict) -> bool:
        """The house's rules, on a TMDB result: no avoided genre, and one of the kept languages."""
        rules = self.rules()
        avoid = {g.lower() for g in rules.get("avoid_genres") or []}
        names = self.genre_names(h["media_type"])
        if avoid & {names.get(g, "").lower() for g in h.get("genre_ids") or []}:
            return False
        keep = rules.get("languages") or []
        return not keep or (h.get("original_language") or "") in keep

    def finish(
        self,
        hits: list[dict],
        why_one: Callable[[dict, list[str]], str],
        keys: list[str],
        limit: int,
        min_votes: int = 0,
        year_from: int | None = None,
    ):
        """Results to picks: watched, 👎, against-the-rules, too few votes (Indian titles need a tenth: TMDB has far
        fewer votes for them) and older than year_from left out; where each plays looked up (the first LOOKUPS only);
        with the apps asked for, only titles on them."""
        seen, disliked = self.seen(), self.disliked()

        def enough(h):
            need = min_votes // 10 if (h.get("original_language") or "") in INDIAN else min_votes
            year = (h.get("release_date") or h.get("first_air_date") or "0")[:4]
            return (h.get("vote_count") or 0) >= need and (not year_from or (year.isdigit() and int(year) >= year_from))

        hits = [
            h
            for h in hits
            if (h.get("title") or h.get("name") or "").lower() not in seen
            and f"{h['media_type']}/{h['id']}" not in disliked
            and h.get("poster_path")
            and enough(h)
            and self.allowed(h)
        ]
        apps_wanted = [a for a in APPS if a.key in keys]

        def one(h):
            try:
                where = self.s.where(h["media_type"], h["id"])
            except StreamingError:
                where = []
            if apps_wanted:
                where = [a for a in where if a in apps_wanted]
                if not where:
                    return None  # TMDB's filter and its per-title list can disagree: trust the per-title one
            names = self.genre_names(h["media_type"])
            item = self.s._item(h, where) | {
                "rating": round(h.get("vote_average") or 0, 1),
                "votes": h.get("vote_count") or 0,
                "language": h.get("original_language") or "",
                "date": h.get("release_date") or h.get("first_air_date") or "",
                "genres": [names[g] for g in h.get("genre_ids") or [] if g in names],
            }
            return item | {"why": why_one(item, [a.name for a in where])}

        with ThreadPoolExecutor(8) as pool:
            return [i for i in pool.map(one, hits[:LOOKUPS]) if i][:limit]

    def app_keys(self, apps: str) -> list[str]:
        """apps: "" for the house's apps, "any" for every app (no filter), or keys ("netflix,prime")."""
        known = {a.key for a in APPS}
        if apps and apps != "any":
            keys = [k.strip() for k in apps.split(",") if k.strip()]
            if set(keys) - known:
                raise RecsError(f"unknown apps; known: {', '.join(sorted(known))}")
            return keys
        return [] if apps == "any" else [a.key for a in self.s.apps]

    def similar(
        self, tmdb: str, apps: str = "", limit: int = 12, min_votes: int = 0, year_from: int | None = None
    ) -> dict:
        """Titles like one title (TMDB's recommendations for it, then its similar list), under the same rules."""
        if not self.s.ready:
            raise RecsError("No TMDB token (tmdb-token): recommendations need it")
        kind, _, tid = tmdb.partition("/")
        if kind not in ("movie", "tv") or not tid.isdigit():
            raise RecsError('tmdb is like "movie/550" or "tv/1396"')
        seed = self.s._get(f"/{kind}/{tid}", language="en-US")
        name = seed.get("title") or seed.get("name") or tmdb
        hits = []
        for path in ("recommendations", "similar"):
            hits += [
                h | {"media_type": kind}
                for h in self.s._get(f"/{kind}/{tid}/{path}", language="en-US").get("results", [])
            ]
        hits = list({h["id"]: h for h in reversed(hits)}.values())[::-1]  # first sighting wins, order kept

        def why_one(item, where):
            r, v = item["rating"], item["votes"]
            return f"Like {name}" + (f"; {r:.1f}/10 from {v:,} votes" if v else "") + "."

        items = self.finish(hits, why_one, self.app_keys(apps), limit, min_votes, year_from)
        return {"like": name, "items": items}

    def details(self, tmdb: str) -> dict:
        """One title, enough to judge it: genres, keywords, who made it and who is in it, length, rating, where
        it plays."""
        if not self.s.ready:
            raise RecsError("No TMDB token (tmdb-token): recommendations need it")
        kind, _, tid = tmdb.partition("/")
        if kind not in ("movie", "tv") or not tid.isdigit():
            raise RecsError('tmdb is like "movie/550" or "tv/1396"')
        h = self.s._get(f"/{kind}/{tid}", language="en-US", append_to_response="keywords,credits")
        if not h.get("id"):
            raise RecsError("TMDB has no such title")
        credits = h.get("credits") or {}
        words = (h.get("keywords") or {}).get("keywords") or (h.get("keywords") or {}).get("results") or []
        try:
            where = self.s.where(kind, int(tid))
        except StreamingError:
            where = []
        item = self.s._item(h | {"media_type": kind}, where)
        made_by = [c["name"] for c in h.get("created_by") or []] or [
            c["name"] for c in credits.get("crew") or [] if c.get("job") == "Director"
        ]
        return item | {
            "genres": [g["name"] for g in h.get("genres") or []],
            "keywords": [w["name"] for w in words][:15],
            "made_by": made_by[:3],
            "cast": [c["name"] for c in (credits.get("cast") or [])[:6]],
            "minutes": h.get("runtime") or next(iter(h.get("episode_run_time") or []), 0),  # a show: per episode
            "seasons": h.get("number_of_seasons") or 0,
            "rating": round(h.get("vote_average") or 0, 1),
            "votes": h.get("vote_count") or 0,
            "language": h.get("original_language") or "",
            "status": h.get("status") or "",
        }

    def genres(self, kind: str) -> dict[str, int]:
        """TMDB's genres for movies or shows, by lower-case name (kept a day)."""
        found = self.s._get(f"/genre/{kind}/list", language="en-US").get("genres", [])
        return {g["name"].lower(): g["id"] for g in found}

    def options(self) -> dict:
        names = sorted({n for k in ("movie", "tv") for n in self.genres(k)}) if self.s.ready else []
        return {
            "categories": {k: c["label"] for k, c in CATEGORIES.items()},
            "apps": [{"key": a.key, "name": a.name} for a in self.s.apps],
            "languages": LANGUAGES,
            "genres": names,
        }

    def recommend(
        self,
        category: str = "worth_the_hype",
        apps: str = "",
        language: str = "any",
        genre: str = "",
        kind: str = "any",
        min_rating: float | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 12,
        today: date | None = None,
        min_votes: int = 0,
    ) -> dict:
        """apps: "" for the house's apps, "any" for every app (no filter), or keys ("netflix,prime")."""
        if not self.s.ready:
            raise RecsError("No TMDB token (tmdb-token): recommendations need it")
        cat = CATEGORIES.get(category)
        if cat is None:
            raise RecsError(f"unknown category; one of: {', '.join(CATEGORIES)}")
        if language != "any" and language not in LANGUAGES:
            raise RecsError(f"unknown language; one of: {', '.join(LANGUAGES)}")
        if kind not in ("any", "movie", "show"):
            raise RecsError("kind is any, movie or show")
        keys = self.app_keys(apps)
        indian = language in INDIAN
        today = today or date.today()

        hits = []
        for k in ("movie", "tv") if kind == "any" else ("movie" if kind == "movie" else "tv",):
            hits += [
                h | {"media_type": k}
                for h in self._discover(k, cat, keys, language, genre, min_rating, year_from, year_to, indian, today)
            ]
        key = "vote_average" if cat["sort"].startswith("vote_average") else "popularity"
        hits.sort(key=lambda h: h.get(key) or 0, reverse=True)
        items = self.finish(hits, lambda item, where: why(category, item, where), keys, limit, min_votes, year_from)
        return {"category": category, "label": cat["label"], "items": items}

    def _discover(self, kind, cat, keys, language, genre, min_rating, year_from, year_to, indian, today) -> list[dict]:
        floor = cat["votes"][1 if indian else 0]
        p = {"sort_by": cat["sort"], "include_adult": "false", "language": "en-US", "vote_count.gte": str(floor)}
        if keys:
            ids = self.s.provider_ids()
            wanted = sorted({i for k in keys for i in ids.get(k, [])})
            if not wanted:
                return []
            p |= {
                "with_watch_providers": "|".join(map(str, wanted)),
                "watch_region": self.s.region,
                "with_watch_monetization_types": "flatrate|free|ads",
            }
        if "max_votes" in cat:
            p["vote_count.lte"] = str(cat["max_votes"][1 if indian else 0])
        low = max(filter(None, [cat.get("min"), min_rating]), default=None)
        if low is not None:
            p["vote_average.gte"] = str(low)
        if "max" in cat:
            p["vote_average.lte"] = str(cat["max"])
        if language != "any":
            p["with_original_language"] = language
        if genre:
            gid = self.genres(kind).get(genre.lower())
            if gid is None:
                return []  # a movie genre with no show twin (or the reverse): nothing of this kind
            p["with_genres"] = str(gid)
        field = "primary_release_date" if kind == "movie" else "first_air_date"
        start = (today - timedelta(days=cat["days"])).isoformat() if "days" in cat else None
        if year_from:
            start = max(filter(None, [start, f"{year_from}-01-01"]))
        if start:
            p[f"{field}.gte"] = start
        p[f"{field}.lte"] = f"{year_to}-12-31" if year_to else today.isoformat()
        return self.s._get(f"/discover/{kind}", **p).get("results", [])


def router(recs: Recs) -> APIRouter:
    api = APIRouter(prefix="/api/recs")

    @api.get("/options")
    def options():
        try:
            return recs.options()
        except StreamingError as e:
            raise HTTPException(502, str(e)) from e

    @api.get("")
    def recommend(
        category: str = "worth_the_hype",
        apps: str = "",
        language: str = "any",
        genre: str = "",
        kind: str = "any",
        min_rating: float | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 12,
        min_votes: int = 0,
    ):
        try:
            return recs.recommend(
                category,
                apps,
                language,
                genre,
                kind,
                min_rating,
                year_from,
                year_to,
                max(1, min(limit, 30)),
                min_votes=max(0, min_votes),
            )
        except RecsError as e:
            raise HTTPException(400, str(e)) from e
        except StreamingError as e:
            raise HTTPException(502, str(e)) from e

    return api
