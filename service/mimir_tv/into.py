"""Into Jellyfin: what mimir-tv puts in the house's Jellyfin library, set in one place (the Settings page's card).

One ordered list of the house's languages (library_languages) drives it all: the whole catalogue keeps titles in
them (mirror.py), On my apps keeps titles first made in them (onapps.py, when onapps_languages), Wholphin's home rows
and the collections follow their order (wholphin.py, grouping.py), and Live TV's tuner asks only for their channel
groups. Each source also has its own numbers: titles per category and episodes per show, titles per app.

The preview says what a choice would hold, and how many titles already in Jellyfin would go to the Trash, before
anything is saved; the jobs (whole catalogue hourly, On my apps daily, or Run now) then make it so."""

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mimir_tv import mirror, onapps, settings
from mimir_tv.onapps import CODES, codes
from mimir_tv.catalog import CatalogError, Catalogs
from mimir_tv.catalog_routes import roots
from mimir_tv.jellyfin import Jellyfin, JellyfinError
from mimir_tv.streaming import StreamingError
from mimir_tv.wizard import allowed


def offered(catalogs: Catalogs) -> list[str]:
    """The languages the whole-catalogue providers list, most titles first."""
    count: dict[str, int] = {}
    for _, cat in catalogs.all():
        try:
            for x in cat.everything():
                if x.get("language"):
                    count[x["language"]] = count.get(x["language"], 0) + 1
        except CatalogError:
            continue
    return sorted(count, key=lambda n: -count[n])


GROUPS = re.compile(r"(?<=[?&])groups=[^&]*")


def set_live_groups(jf: Jellyfin, groups: list[str]) -> int:
    """Live TV's tuners that ask for channel groups (a groups= in their address, as a provider plugin's playlist
    takes it) ask for these instead; returns how many changed. Others are left alone."""
    if not groups:
        return 0
    want = "groups=" + quote(",".join(groups), safe=",")
    changed = 0
    for t in (jf._call("GET", "/System/Configuration/livetv") or {}).get("TunerHosts", []):
        url = t.get("Url") or ""
        if GROUPS.search(url) and GROUPS.sub(want, url) != url:
            jf._call("POST", "/LiveTv/TunerHosts", body=t | {"Url": GROUPS.sub(want, url)})
            changed += 1
    return changed


class Choice(BaseModel):
    languages: list[str] = Field(default_factory=list, max_length=30)
    mirror: dict[str, bool] = Field(default_factory=dict)  # provider id -> whole catalogue on or off
    per_category: int = Field(0, ge=0, le=100_000)
    episodes: int = Field(10, ge=1, le=10_000)
    onapps: bool = True
    onapps_per_app: int = Field(3000, ge=1, le=20_000)
    onapps_languages: bool = True
    wholphin_rows: bool = True
    wholphin_channels: list[str] = Field(default_factory=list, max_length=50)


def preview(c: Choice, catalogs: Catalogs, jf: Jellyfin, media_root: Path, streaming) -> dict:
    """What the choice would hold: per whole-catalogue provider, movies and shows wanted, now in Jellyfin, and to
    the Trash (watched, favourited and added by hand stay); On my apps, about how many titles."""
    try:
        kept = jf.kept_paths()
    except JellyfinError:
        kept = []
    out: dict = {"catalogue": [], "apps": None}
    for entry, cat in catalogs.all():
        if not c.mirror.get(entry["id"], bool(entry.get("mirror"))):
            continue
        try:
            listed = cat.everything()
        except CatalogError:
            continue
        if not listed:
            continue
        want = mirror.wanted(listed, c.languages, c.per_category)
        keep = {x["url"] for x in want}
        where, row = roots(media_root, entry["id"]), {"provider": entry["name"]}
        for kind in ("movies", "shows"):
            root = where[kind]
            row[kind] = {
                "wanted": sum(1 for x in want if x["kind"] == kind[:-1]),
                "now": len(mirror.mirrored(root)),
                "to_trash": mirror.prune(root, keep, mirror.names_in(root, kept), root, dry=True),
            }
        out["catalogue"].append(row)
    if c.onapps and streaming is not None and streaming.ready:
        langs = codes(c.languages) if c.onapps_languages else []
        try:
            ids = streaming.provider_ids()
            about = sum(
                min(c.onapps_per_app, streaming.on_app_count(ids[a.key], langs or None))
                for a in streaming.apps
                if ids.get(a.key)
            )
        except StreamingError:
            about = None
        out["apps"] = {"about": about, "now": len(onapps.state(media_root / "apps"))}
    return out


def router(config: Path, catalogs: Catalogs, jf: Jellyfin, media_root: Path, streaming) -> APIRouter:
    change = APIRouter(dependencies=[Depends(allowed)])

    @change.get("/api/settings/into")
    def current():
        cfg = settings.load(config)
        providers = []
        for entry, cat in catalogs.all():
            try:
                can = bool(cat.everything())
            except CatalogError:
                can = False
            providers.append(
                {"id": entry["id"], "name": entry["name"], "mirror": bool(entry.get("mirror")), "can": can}
            )
        return {
            "languages": cfg["library_languages"],
            "offered": offered(catalogs),
            "tmdb_languages": sorted(CODES),
            "providers": providers,
            "per_category": cfg["mirror_per_category"],
            "episodes": cfg["mirror_episodes"],
            "onapps": cfg["onapps"],
            "onapps_per_app": cfg["onapps_per_app"],
            "onapps_languages": cfg["onapps_languages"],
            "tmdb": streaming is not None and streaming.ready,
            "wholphin_rows": cfg["wholphin_rows"],
            "wholphin_channels": cfg["wholphin_channels"],
        }

    @change.post("/api/settings/into/preview")
    def what_if(c: Choice):
        return preview(c, catalogs, jf, media_root, streaming)

    @change.put("/api/settings/into")
    def save(c: Choice):
        """Saved at once; the jobs apply it (whole catalogue hourly, On my apps daily, or Run now). Live TV's channel
        groups change in Jellyfin straight away."""
        langs = [n.strip() for n in c.languages if n.strip()]
        if len(set(langs)) != len(langs):
            raise HTTPException(422, "a language is there twice")
        cfg = settings.load(config)
        providers = [p | {"mirror": c.mirror.get(p["id"], bool(p.get("mirror")))} for p in cfg["providers"]]
        settings.save(
            {
                "library_languages": langs,
                "providers": providers,
                "mirror_per_category": c.per_category,
                "mirror_episodes": c.episodes,
                "onapps": c.onapps,
                "onapps_per_app": c.onapps_per_app,
                "onapps_languages": c.onapps_languages,
                "wholphin_rows": c.wholphin_rows,
                "wholphin_channels": [n.strip() for n in c.wholphin_channels if n.strip()],
            },
            config,
        )
        try:
            live = set_live_groups(jf, langs)
        except JellyfinError:
            live = None
        return {"live_tuners_changed": live}

    return change
