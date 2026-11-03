"""D8: mimir-flix as an MCP server, so an assistant (OpenClaw on the Air) can find something and play it on the TV.

One endpoint, POST /mcp (MCP's streamable HTTP transport, answered as plain JSON; no event stream). Every call needs
the bearer token from ~/.config/mimir-flix/mcp-token (owner-only, made on first start). Through Caddy only tailnet
devices get in; OpenClaw on the Air itself comes straight to 127.0.0.1.

The tools call mimir-flix's own routes in-process, so they search, play and add exactly as the page does.
"""

import asyncio
import hmac
import json
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from mimir_flix.hls import from_tailnet
from mimir_flix.streaming import APPS
from mimir_flix.tv import TvError

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first; the client's own is answered when known
DESC = 200  # characters of each description: enough to recommend from

# What the TV shows, by package: the streaming apps and the rest of the house's TV apps.
TV_APPS = {a.package: a.name for a in APPS} | {
    "org.jellyfin.androidtv": "Jellyfin",
    "org.xbmc.kodi": "Kodi",
    "com.gathin.tvhome": "TV Home",
    "dev.mimirflix.tv": "mimir-flix",
    "com.spotify.tv.android": "Spotify",
}

ASK_FIRST = "Call whats_on_tv first; if something is playing, ask the user before changing it."
TOOLS = [
    {
        "name": "search",
        "description": "Find a movie, show or video. Searches the home library (Jellyfin), the house's streaming apps "
        "(Netflix, Prime Video, Peacock …, with which apps have it) and the house's providers (their shows, movies and "
        "live channels; each result has provider and id).",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "play_on_tv",
        "description": "Play on the living-room TV. Give exactly one of: jellyfin_id (from search's library), app "
        "with title and tmdb (from search's apps: opens that app at the title), or provider and id with title (a "
        f"provider's video, from search or live_now). via picks the player for jellyfin_id and a provider's video: "
        f"jellyfin (default) or kodi. {ASK_FIRST}",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jellyfin_id": {"type": "string"},
                "app": {"type": "string", "description": "the app's key, e.g. netflix"},
                "tmdb": {"type": "string", "description": "e.g. tv/96677"},
                "provider": {"type": "string"},
                "id": {"type": "string", "description": "a provider's video id"},
                "page": {"type": "string", "description": "with an id from live_now: its list"},
                "title": {"type": "string"},
                "via": {"type": "string", "enum": ["jellyfin", "kodi"]},
            },
        },
    },
    {
        "name": "whats_on_tv",
        "description": "Whether the TV's screen is on, which app is in front, and, when it is Jellyfin, what it is playing (title, episode, paused, minute).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "live_now",
        "description": "Live streams showing a match or channel right now, best first (up to 3, each with why): "
        "e.g. 'Arsenal', 'Arsenal Chelsea', 'Star Sports'. Play the first with play_on_tv (provider, id, page, title) "
        f"and keep the others in case it fails. Takes up to half a minute. {ASK_FIRST}",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "whats_new",
        "description": "New episodes: shows on My list with episodes added since they were opened, and episodes the "
        "home library (Jellyfin) added in the last days (play those with play_on_tv jellyfin_id).",
        "inputSchema": {"type": "object", "properties": {"days": {"type": "integer", "description": "default 7"}}},
    },
    {
        "name": "recommend",
        "description": "Suggest what to watch. Start from what the person asks for and turn it into a category and "
        "filters: buzzing (popular new), worth_the_hype (popular and well rated), skip_the_hype (popular, poorly "
        "rated; only when asked), crowd_favourites (highly rated by many, any year), hidden_gems (highly rated, few "
        "votes), new_on_your_apps (last 30 days). apps: empty for the house's apps, 'any', or keys like "
        "'netflix,prime'. language: en, hi, te, ta, ml, kn, ko, ja, es … or any. Each pick has a rating, a one-line "
        "why and where it plays (play it with play_on_tv app/title/tmdb). Titles already watched are left out.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "buzzing",
                        "worth_the_hype",
                        "skip_the_hype",
                        "crowd_favourites",
                        "hidden_gems",
                        "new_on_your_apps",
                    ],
                },
                "apps": {"type": "string"},
                "language": {"type": "string"},
                "genre": {"type": "string", "description": "e.g. comedy, drama, thriller, animation"},
                "kind": {"type": "string", "enum": ["any", "movie", "show"]},
                "min_rating": {"type": "number"},
                "year_from": {"type": "integer"},
                "year_to": {"type": "integer"},
                "min_votes": {"type": "integer", "description": "e.g. 200 (Indian titles need a tenth)"},
            },
            "required": ["category"],
        },
    },
    {
        "name": "taste",
        "description": "What the house likes, to recommend from: genres and people weighted by what was watched in "
        "the home library (favourites and long-watched shows count more), liked titles (👍, favourites, watched far) "
        "with TMDB ids, disliked titles (👎), and the house's rules (genres to avoid, languages to keep), which "
        "candidates, similar_to and recommend already apply. The person's own instructions are not here: they are "
        "the assistant's to keep.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "history",
        "description": "Movies and shows watched in the home library, most recent first: title, kind, year, tmdb, "
        "genres, episodes watched (shows), favourite, last played date. Home videos are never included.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "default 30"}}},
    },
    {
        "name": "candidates",
        "description": "A wide pool to choose recommendations from (up to 60): the recommendation categories plus "
        "titles like the best-liked ones, on the house's apps unless apps says otherwise. Watched, 👎 and "
        "against-the-rules titles are already out. Each has rating, votes, genres, language, apps, tmdb, desc and "
        "reasons (every list it came up in). Rank them yourself with the person's instructions and show them in the chat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "apps": {"type": "string", "description": "empty: the house's apps; 'any'; or keys like 'netflix'"},
                "kind": {"type": "string", "enum": ["any", "movie", "show"]},
                "limit": {"type": "integer"},
                "min_votes": {
                    "type": "integer",
                    "description": "default 200: thinly rated titles are left out (Indian titles need a tenth); 0 for all",
                },
                "year_from": {"type": "integer", "description": "only titles from this year on (new titles)"},
                "language": {"type": "string", "description": "one original language for the categories: te, hi …"},
            },
        },
    },
    {
        "name": "find_titles",
        "description": "Look up many titles at once by name and year (e.g. every title a review list mentions): each "
        "found one with tmdb, rating, votes, genres, language and where it plays, under the house's rules. "
        "not_found: TMDB does not know it; left_out: watched, 👎 or against the rules.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "titles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "year": {"type": "integer"},
                            "kind": {"type": "string", "enum": ["movie", "show"]},
                        },
                        "required": ["title"],
                    },
                }
            },
            "required": ["titles"],
        },
    },
    {
        "name": "similar_to",
        "description": "Titles like one title (tmdb from search, history or taste, e.g. tv/70523), under the same "
        "rules: for 'something like Dark'.",
        "inputSchema": {
            "type": "object",
            "properties": {"tmdb": {"type": "string"}, "apps": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["tmdb"],
        },
    },
    {
        "name": "title_details",
        "description": "One title in full, to judge it against what the person wants: genres, keywords, made by, "
        "cast, minutes (a show: per episode), seasons, rating, votes, language, status, where it plays.",
        "inputSchema": {"type": "object", "properties": {"tmdb": {"type": "string"}}, "required": ["tmdb"]},
    },
    {
        "name": "feedback",
        "description": "Record what the person said about a title: up (liked it, more like it), down (not for them: "
        "left out of every recommendation from now on), or clear.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tmdb": {"type": "string"},
                "vote": {"type": "string", "enum": ["up", "down", "clear"]},
                "title": {"type": "string"},
            },
            "required": ["tmdb", "vote"],
        },
    },
    {
        "name": "trending",
        "description": "This week's most watched movies and shows that stream on the house's apps.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_to_jellyfin",
        "description": "Add a provider's show (provider and id from search) to the home library; new episodes follow "
        "by themselves. Returns its jellyfin_id for play_on_tv.",
        "inputSchema": {
            "type": "object",
            "properties": {"provider": {"type": "string"}, "id": {"type": "string"}, "title": {"type": "string"}},
            "required": ["id", "title"],
        },
    },
    {
        "name": "my_list",
        "description": "The house's My list, newest first: titles in the home library (jellyfin_id), titles on the "
        "streaming apps (tmdb, apps) and providers' shows and videos (provider, id; with new-episode counts).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_to_my_list",
        "description": "Save to My list. Streaming titles: tmdb_ids, one or several at once (e.g. after showing "
        "recommendations, add the ones the person chose, or all of them). Titles in the home library: jellyfin_ids "
        "(from search's library). A provider's show or video from search: provider and id, one, with its title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tmdb_ids": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['tv/1396']"},
                "jellyfin_ids": {"type": "array", "items": {"type": "string"}},
                "provider": {"type": "string"},
                "id": {"type": "string"},
                "title": {"type": "string"},
            },
        },
    },
    {
        "name": "remove_from_my_list",
        "description": "Take a title off My list: tmdb for a streaming title, jellyfin_id for one in the home "
        "library, provider and id for a provider's one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tmdb": {"type": "string"},
                "jellyfin_id": {"type": "string"},
                "provider": {"type": "string"},
                "id": {"type": "string"},
            },
        },
    },
]


def lean(i: dict) -> dict:
    """A recommendation without what only the page needs (pictures, app links): smaller for the model."""
    out = {k: v for k, v in i.items() if k not in ("thumb", "backdrop", "date")}
    return out | {"apps": [x["app"] for x in i["apps"]], "desc": short(i["desc"])}


def my_list_args(a: dict) -> dict:
    """The My list tools under the names a model tends to guess (a model gave up after tmdb= and a url= failed): one
    id or a list, singular or plural; a provider's item as id, node, url or any *_url."""
    a = item_args(a)
    for one, many in (("tmdb", "tmdb_ids"), ("tmdb_id", "tmdb_ids"), ("jellyfin_id", "jellyfin_ids")):
        if a.get(one) and not a.get(many):
            a[many] = a[one] if isinstance(a[one], list) else [a[one]]
    for one, many in (("tmdb", "tmdb_ids"), ("jellyfin_id", "jellyfin_ids")):
        if a.get(many) and not a.get(one):
            a[one] = a[many][0] if isinstance(a[many], list) else a[many]
    return a


def item_args(a: dict) -> dict:
    """A provider's item under the names a model guesses: id, node, url or any *_url (older tools' names)."""
    if not a.get("id"):
        guess = a.get("node") or a.get("url") or next((v for k, v in a.items() if k.endswith("_url") and v), "")
        if guess:
            a = a | {"id": guess}
    return a


class ToolError(Exception):
    pass


def short(text: str) -> str:
    return text if len(text) <= DESC else text[: DESC - 1].rstrip() + "…"


def on_apps(found: list[dict]) -> list[dict]:
    return [
        {
            "title": t["title"],
            "year": t["year"],
            "kind": t["kind"],
            "tmdb": t["tmdb"],
            "apps": [a["app"] for a in t["apps"]],
            "desc": short(t["desc"]),
        }
        for t in found
    ]


def router(token: str, tv_now: Callable[[], dict] | None = None) -> APIRouter:
    api = APIRouter()

    async def call(app, method: str, path: str, **kw) -> Any:
        """One of mimir-flix's own routes, in-process; its error detail becomes the tool's error."""
        # In-process, as this Mac (the app answers only to its own names: an address is one).
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            r = await c.request(method, path, timeout=60, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            raise ToolError(detail if isinstance(detail, str) else f"{path} answered {r.status_code}")
        return r.json() if r.content else {}

    async def providers(app) -> list[dict]:
        try:
            return (await call(app, "GET", "/api/connectors")).get("providers") or []
        except ToolError:
            return []  # an app without providers

    async def which(app, a: dict) -> str:
        """The provider an item belongs to: the one given, or the first (a model that left it out)."""
        found = await providers(app)
        if not found:
            raise ToolError("no provider is set up")
        if a.get("provider"):
            # Only a provider's id: the id goes into a path, so anything else could reach another route.
            if a["provider"] not in {p["id"] for p in found}:
                raise ToolError("no such provider (search gives each result's provider)")
            return a["provider"]
        return found[0]["id"]

    async def search(app, query: str) -> dict:
        async def one(path):
            try:
                return await call(app, "GET", path, params={"q": query})
            except ToolError as e:
                return e

        found = await providers(app)
        lib, apps, *mine = await asyncio.gather(
            one("/api/jf/search"), one("/api/streaming/search"), *[one(f"/api/p/{p['id']}/search") for p in found]
        )
        out: dict[str, Any] = {}
        if isinstance(lib, list):
            out["library"] = [
                {
                    "jellyfin_id": i["id"],
                    "title": i["title"],
                    "kind": i["type"],
                    "sub": i["sub"],
                    "desc": short(i["desc"]),
                }
                for i in lib[:10]
            ]
        if isinstance(apps, list):
            out["apps"] = on_apps(apps)
        groups = [
            {
                "provider": p["id"],
                "name": p["name"],
                "section": g["section"],
                "items": [
                    {"title": i["title"], "sub": i["sub"], "id": i.get("node") or i["url"]}
                    | {"kind": "show" if i["kind"] == "folder" else "video"}
                    | ({"jellyfin_id": i["jf_id"]} if i["jf_id"] else {})
                    for i in g["items"][:5]
                ],
            }
            for p, res in zip(found, mine)
            if isinstance(res, list)
            for g in res[:6]
        ]
        if groups:
            out["providers"] = groups
        failed = [n for n, r in (("library", lib), ("apps", apps)) if isinstance(r, ToolError)]
        if failed:
            out["unavailable"] = failed
        return out

    async def play_on_tv(app, a: dict) -> dict:
        a = item_args(a)
        given = [k for k in ("jellyfin_id", "app", "id") if a.get(k)]
        if len(given) != 1:
            raise ToolError("give exactly one of jellyfin_id, app or a provider's id")
        kodi = a.get("via") == "kodi"
        title = (a.get("title") or "")[:200]
        if a.get("jellyfin_id"):
            return await call(app, "POST", "/api/jf/kodi" if kodi else "/api/jf/tv", json={"id": a["jellyfin_id"]})
        if a.get("app"):
            await call(
                app, "POST", "/api/streaming/open", json={"app": a["app"], "title": title, "tmdb": a.get("tmdb", "")}
            )
            return {"opened": a["app"], "title": title}
        pid = await which(app, a)
        video = {"url": a["id"], "title": title or "Video", "page": a.get("page") or ""}
        if kodi:
            return await call(app, "POST", f"/api/p/{pid}/kodi", json=video)
        added = await call(app, "POST", f"/api/p/{pid}/jellyfin/video", json=video | {"tv": True})
        return {"playing": video["title"], "jellyfin_id": added["id"]}

    async def saved(app) -> list[dict]:
        """Every provider's My list entries, each with provider."""
        out = []
        for p in await providers(app):
            try:
                out += [t | {"provider": p["id"]} for t in await call(app, "GET", f"/api/p/{p['id']}/list")]
            except ToolError:
                continue  # that provider is not answering: the rest of the list still answers
        return out

    async def whats_on_tv(app) -> dict:
        if tv_now is None:
            raise ToolError("No TV set up (add one on the Settings page)")
        try:
            now = await asyncio.to_thread(tv_now)
        except TvError as e:
            raise ToolError(str(e)) from e
        out = now | {"app_name": TV_APPS.get(now["app"], now["app"])}
        if now["app"] == "org.jellyfin.androidtv":  # Jellyfin knows what its TV app is playing
            try:
                playing = await call(app, "GET", "/api/jf/now")
            except ToolError:
                playing = None
            out["playing"] = "unknown" if playing is None else (playing or "nothing (browsing)")
        return out

    async def whats_new(app, days: int) -> dict:
        out: dict[str, Any] = {}
        listed = [
            {"title": t["title"], "new_episodes": t["new"], "provider": t["provider"], "id": t["key"]}
            for t in await saved(app)
            if t.get("new")
        ]
        if listed:
            out["my_list"] = listed
        try:
            out["library"] = [
                {
                    "jellyfin_id": e["id"],
                    "show": e["show"],
                    "episode": e["episode"],
                    "title": e["title"],
                    "added": e["added"],
                }
                for e in await call(app, "GET", "/api/jf/new", params={"days": days})
            ]
        except ToolError:
            out["unavailable"] = ["library"]
        return out

    async def run(app, name: str, a: dict) -> Any:
        if name == "search":
            if len((a.get("query") or "").strip()) < 2:
                raise ToolError("query needs at least 2 characters")
            return await search(app, a["query"].strip())
        if name == "play_on_tv":
            return await play_on_tv(app, a)
        if name == "whats_on_tv":
            return await whats_on_tv(app)
        if name == "trending":
            return on_apps(await call(app, "GET", "/api/streaming/trending"))
        if name == "live_now":
            if len((a.get("query") or "").strip()) < 2:
                raise ToolError("query needs at least 2 characters")
            found = []
            for p in await providers(app):
                try:
                    found += await call(app, "GET", f"/api/p/{p['id']}/live", params={"q": a["query"].strip()})
                except ToolError:
                    continue
            return [
                {
                    "title": s["title"],
                    "section": s["section"],
                    "provider": s["p"],
                    "id": s["url"],
                    "page": s["page"],
                    "why": s["why"],
                }
                for s in found
            ] or "Nothing live matches that right now."
        if name == "recommend":
            q = {
                k: a[k]
                for k in (
                    "category",
                    "apps",
                    "language",
                    "genre",
                    "kind",
                    "min_rating",
                    "year_from",
                    "year_to",
                    "min_votes",
                )
                if a.get(k) not in (None, "")
            }
            found = await call(app, "GET", "/api/recs", params=q | {"limit": 8})
            return [
                {
                    "title": i["title"],
                    "year": i["year"],
                    "kind": i["kind"],
                    "rating": i["rating"],
                    "votes": i["votes"],
                    "why": i["why"],
                    "apps": [x["app"] for x in i["apps"]],
                    "tmdb": i["tmdb"],
                    "desc": short(i["desc"]),
                }
                for i in found["items"]
            ] or f"Nothing in {found['label']} with those filters; try fewer filters or another category."
        if name == "taste":
            return await call(app, "GET", "/api/taste")
        if name == "history":
            return await call(app, "GET", "/api/taste/history", params={"limit": int(a.get("limit") or 30)})
        if name == "candidates":
            q = {
                k: a[k]
                for k in ("apps", "kind", "limit", "min_votes", "year_from", "language")
                if a.get(k) not in (None, "")
            }
            return [lean(i) for i in await call(app, "GET", "/api/recs/candidates", params=q)] or (
                "No candidates: is the TMDB token set up (Settings)?"
            )
        if name == "find_titles":
            titles = a["titles"] if isinstance(a["titles"], list) else []
            found = await call(app, "POST", "/api/recs/find", json={"titles": titles[:30]})
            return found | {"items": [lean(i) for i in found["items"]]}
        if name == "similar_to":
            q = {"tmdb_id": a["tmdb"]} | {k: a[k] for k in ("apps", "limit") if a.get(k) not in (None, "")}
            found = await call(app, "GET", "/api/recs/similar", params=q)
            return {"like": found["like"], "items": [lean(i) for i in found["items"]]}
        if name == "title_details":
            d = await call(app, "GET", "/api/recs/details", params={"tmdb_id": a["tmdb"]})
            return {k: v for k, v in d.items() if k not in ("thumb", "backdrop")} | {
                "apps": [x["app"] for x in d["apps"]]
            }
        if name == "feedback":
            body = {"tmdb": a["tmdb"], "vote": a["vote"], "title": (a.get("title") or "")[:200]}
            await call(app, "POST", "/api/recs/feedback", json=body)
            return {"recorded": a["vote"], "tmdb": a["tmdb"]}
        if name == "whats_new":
            return await whats_new(app, int(a.get("days") or 7))
        if name == "add_to_jellyfin":
            a = item_args(a)
            body = {"node": a["id"], "title": a["title"]}
            added = await call(app, "POST", f"/api/p/{await which(app, a)}/jellyfin", json=body)
            return {"jellyfin_id": added["id"], "episodes": added["episodes"]}
        if name == "my_list":
            try:
                library = await call(app, "GET", "/api/jf/list")
            except ToolError:
                library = []  # no Jellyfin here: the rest of the list still answers
            out: dict[str, Any] = {
                "library": [{"title": t["title"], "sub": t["sub"], "jellyfin_id": t["id"]} for t in library],
                "titles": [
                    {"title": t["title"], "year": t["year"], "kind": t["kind"], "tmdb": t["tmdb"]}
                    | {"apps": [x["app"] for x in t["apps"]]}
                    for t in await call(app, "GET", "/api/list/titles")
                ],
            }
            mine = await saved(app)
            if mine:
                out["providers"] = [
                    {"title": t["title"], "kind": "show" if t["kind"] == "folder" else "video", "sub": t["sub"]}
                    | {"provider": t["provider"], "id": t["key"]}
                    | ({"new_episodes": t["new"]} if t.get("new") else {})
                    for t in mine
                ]
            return out
        if name in ("add_to_my_list", "remove_from_my_list"):
            a = my_list_args(a)
        if name == "remove_from_my_list":
            if a.get("jellyfin_id"):
                await call(app, "DELETE", "/api/jf/list", params={"id": a["jellyfin_id"]})
            elif a.get("tmdb"):
                await call(app, "DELETE", "/api/list/titles", params={"tmdb": a["tmdb"]})
            elif a.get("id"):
                await call(app, "DELETE", f"/api/p/{await which(app, a)}/list", params={"key": a["id"]})
            else:
                raise ToolError("give tmdb, jellyfin_id, or a provider's id")
            return {"removed": a.get("jellyfin_id") or a.get("tmdb") or a.get("id")}
        # add_to_my_list
        ids, lib = a.get("tmdb_ids"), a.get("jellyfin_ids")
        ids = [ids] if isinstance(ids, str) else ids
        lib = [lib] if isinstance(lib, str) else lib
        if ids or lib:
            out = {"added": [], "already": []}
            for jid in lib or []:
                got = await call(app, "POST", "/api/jf/list", json={"id": jid})
                out = {k: out[k] + got[k] for k in out}
            if ids:
                got = await call(app, "POST", "/api/list/titles", json={"tmdb": ids})
                out = {k: out[k] + got[k] for k in out}
            return out
        if not a.get("title") or not a.get("id"):
            raise ToolError("give tmdb_ids (e.g. ['movie/1511417']) or jellyfin_ids, or a provider's id with its title")
        video = a["id"].startswith("https://")
        body = {"node": "" if video else a["id"], "url": a["id"] if video else "", "title": a["title"]}
        return await call(app, "POST", f"/api/p/{await which(app, a)}/list", json=body)

    def answer(id_, result=None, error: tuple[int, str] | None = None) -> JSONResponse:
        body = {"jsonrpc": "2.0", "id": id_}
        body |= {"error": {"code": error[0], "message": error[1]}} if error else {"result": result}
        return JSONResponse(body)

    @api.post("/mcp")
    async def mcp(request: Request):
        peer = request.client.host if request.client else ""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded and not from_tailnet(peer, forwarded):  # through Caddy from the LAN (the TV, guests)
            return Response(status_code=403)
        sent = request.headers.get("authorization", "")
        if not token or not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        try:
            msg = await request.json()
        except ValueError:
            return answer(None, error=(-32700, "not JSON"))
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return answer(None, error=(-32600, "not a JSON-RPC request"))
        if "id" not in msg:  # a notification (initialized, cancelled): nothing to answer
            return Response(status_code=202)
        id_, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return answer(
                id_,
                {
                    "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mimir-flix", "version": "0.1.0"},
                    "instructions": "Find and play movies, shows and live TV on the living-room TV. " + ASK_FIRST,
                },
            )
        if method == "ping":
            return answer(id_, {})
        if method == "tools/list":
            return answer(id_, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if tool is None:
                return answer(id_, error=(-32602, f"no tool {name}"))
            missing = [k for k in tool["inputSchema"].get("required", []) if not args.get(k)]
            try:
                if missing:
                    raise ToolError(f"missing {', '.join(missing)}")
                result, failed = await run(request.app, name, args), False
            except ToolError as e:
                result, failed = str(e), True
            text = result if failed else json.dumps(result, ensure_ascii=False)
            return answer(id_, {"content": [{"type": "text", "text": text}], "isError": failed})
        return answer(id_, error=(-32601, f"no method {method}"))

    @api.get("/mcp")
    def no_stream():
        return Response(status_code=405, headers={"Allow": "POST"})  # no server-sent events: every answer is JSON

    return api
