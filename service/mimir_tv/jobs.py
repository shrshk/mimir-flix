"""mimir-tv's scheduled jobs, each run recorded with jobkit in mimir-tv's own database; the jobs page reads
GET /api/jobs."""

import sqlite3
from pathlib import Path

import httpx

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from jobkit import Jobs, page_html

from mimir_tv import grouping, mirror, onapps, settings, wholphin
from mimir_tv.catalog import CatalogError, Catalogs, Job
from mimir_tv.catalog_routes import libraries, roots
from mimir_tv.jellyfin import Jellyfin, JellyfinError
from mimir_tv.strm import refresh_shows
from mimir_tv.wizard import allowed

NEW_EPISODES = "new-episodes"
WHOLE_CATALOGUE = "whole-catalogue"
TRASH = Path.home() / ".Trash"  # where titles outside the whole catalogue's limit go (mirror.prune)
ON_MY_APPS = "on-my-apps"


def define(jobs: Jobs) -> None:
    jobs.define(NEW_EPISODES, "New episodes: added shows into Jellyfin", "hourly")
    jobs.define(ON_MY_APPS, "On my apps: what is popular on the house's streaming apps, into Jellyfin", "daily")
    jobs.define(
        WHOLE_CATALOGUE, "Whole catalogue: every movie and show into Jellyfin, a batch of shows a run", "hourly"
    )


def new_episodes(jobs: Jobs, catalogs: Catalogs, jf: Jellyfin, media_root: Path, resolver: str) -> int:
    """New episodes of every provider's followed shows into their folders, then a scan so Jellyfin lists them."""
    with jobs.run(NEW_EPISODES) as run:
        found = 0
        for entry, cat in catalogs.all():
            new = refresh_shows(cat, entry["id"], roots(media_root, entry["id"])["shows"], resolver)
            if new:
                lib = next((x for x in jf.libraries() if x["Name"] == libraries(entry)["shows"]), None)
                if lib:
                    jf.scan(lib.get("ItemId"))
            found += new
        run.summary = f"{found} new episode{'s' if found != 1 else ''}"
        return found


def whole_catalogue(
    jobs: Jobs, catalogs: Catalogs, jf: Jellyfin, media_root: Path, resolver: str, trash: Path = TRASH
) -> str:
    """Every provider that turned it on ("mirror": true in its settings entry): its whole catalogue into its
    Jellyfin libraries, a batch of shows a run (mirror.py), then a scan of the libraries that changed (made when
    missing)."""
    with jobs.run(WHOLE_CATALOGUE) as run:
        notes = []
        cfg = settings.load(catalogs.config)
        for entry, cat in catalogs.all():
            if not entry.get("mirror"):
                continue
            where, names = roots(media_root, entry["id"]), libraries(entry)
            mirror.follow_favourites(where["shows"], jf.favourite_show_paths())  # all their episodes from now
            langs, cap = cfg["library_languages"], cfg["mirror_per_category"]
            done = mirror.mirror(
                cat,
                entry["id"],
                where["shows"],
                where["movies"],
                resolver,
                languages=langs,
                per_category=cap,
                episodes=cfg["mirror_episodes"],
            )
            removed = {"shows": 0, "movies": 0}
            if langs or cap:
                keep = {x["url"] for x in mirror.wanted(cat.everything(), langs, cap)}
                kept = jf.kept_paths()
                for kind in removed:
                    removed[kind] = mirror.prune(where[kind], keep, mirror.names_in(where[kind], kept), trash)
            have = {x["Name"]: x for x in jf.libraries()}
            touched = done["details"] or done["posters"]  # a changed .nfo or a new poster: Jellyfin reads them again
            for kind, jf_kind, wrote in (
                ("shows", "tvshows", done["shows"] or removed["shows"] or touched),
                ("movies", "movies", done["movies"] or removed["movies"] or touched),
            ):
                if not wrote:
                    continue
                if names[kind] in have:
                    jf.scan(have[names[kind]].get("ItemId"))
                else:
                    jf.create_library(names[kind], jf_kind, [str(where[kind])])
            waiting = mirror.left(cat, where["shows"], langs, cap)
            notes.append(
                f"{entry['name']}: {done['movies']} movies, {done['shows']} shows ({done['episodes']} episodes)"
                + (f", {done['failed']} failed" if done["failed"] else "")
                + (
                    f", {done['details']} details and {done['posters']} posters"
                    if done["details"] or done["posters"]
                    else ""
                )
                + (
                    f", {removed['movies']} movies and {removed['shows']} shows to the Trash"
                    if any(removed.values())
                    else ""
                )
                + (f", {waiting} shows waiting" if waiting else "")
            )
            if langs:  # the house's languages: Wholphin's rows and the collections follow them
                try:
                    if cfg["wholphin_rows"]:
                        notes.append("Wholphin home: " + wholphin.home(jf, names["shows"], names["movies"], cfg))
                    have = {x["Name"]: x.get("ItemId") for x in jf.libraries()}
                    if have.get(names["shows"]) and have.get(names["movies"]):
                        notes.append(
                            "Collections: " + grouping.group(jf, have[names["shows"]], have[names["movies"]], langs)
                        )
                except JellyfinError as e:  # the titles are in; the layout is tried again next run
                    notes.append(f"Wholphin home and collections not updated: {e}")
        run.summary = "; ".join(notes) or "no provider has the whole catalogue turned on"
        return run.summary


def on_my_apps(jobs: Jobs, streaming, jf: Jellyfin, media_root: Path, base: str, cfg: dict | None = None) -> str:
    """The streaming apps' popular titles into the On my apps library (onapps.py), made when missing; how many and
    which languages come from Settings (Into Jellyfin)."""
    cfg = cfg or settings.DEFAULTS
    with jobs.run(ON_MY_APPS) as run:
        if not cfg["onapps"]:
            run.summary = "off (Settings, Into Jellyfin): titles already there go after two weeks"
            return run.summary
        if not streaming.ready:
            run.summary = "no TMDB token (Settings)"
            return run.summary
        root = media_root / "apps"
        langs = onapps.codes(cfg["library_languages"]) if cfg["onapps_languages"] else []
        done = onapps.fill(streaming, root, base, per_app=cfg["onapps_per_app"], languages=langs or None)
        lib = next((x for x in jf.libraries() if x["Name"] == onapps.LIBRARY), None)
        if lib is None:
            jf.create_library(onapps.LIBRARY, "movies", [str(root)])
        elif done["new"] or done["removed"] or done["posters"]:
            jf.scan(lib.get("ItemId"))
        lists = onapps.playlists(streaming, jf, root)
        run.summary = (
            f"{done['titles']} titles ({done['new']} new, {done['removed']} removed), {lists} app lists updated"
        )
        return run.summary


def job_name(pid: str, job: Job) -> str:
    return f"{pid}-{job.key}"


def provider_jobs(jobs: Jobs, catalogs: Catalogs) -> list[tuple[str, Job]]:
    """Every provider's own jobs, defined and registered with jobkit (Run now on the jobs page runs them either way);
    a provider added since the last call gets its jobs here."""
    out = []
    for entry, cat in catalogs.all():
        for job in cat.jobs():
            name = job_name(entry["id"], job)
            jobs.define(name, f"{entry['name']}: {job.description}", job.schedule)
            jobs.register(name, lambda name=name, job=job: run_job(jobs, name, job))
            out.append((name, job))
    return out


def run_job(jobs: Jobs, name: str, job: Job) -> None:
    """One run, recorded; a failure is recorded as failed by jobkit and tried again next round."""
    try:
        with jobs.run(name) as run:
            run.summary = job.run()
    except (CatalogError, httpx.HTTPError, sqlite3.Error, OSError) as e:
        print(f"{name} failed:", e)


class Pause(BaseModel):
    paused: bool = True


def router(jobs: Jobs) -> APIRouter:
    api = APIRouter()

    @api.get("/api/jobs")
    def all_jobs():
        return {"app": "mimir-tv", "jobs": jobs.report()}

    @api.post("/api/jobs/{name}/run", status_code=202, dependencies=[Depends(allowed)])
    def run_now(name: str):
        try:
            jobs.run_now(name)
        except LookupError as e:
            raise HTTPException(404, "no such job, or nothing to start") from e

    @api.post("/api/jobs/{name}/pause", status_code=204, dependencies=[Depends(allowed)])
    def pause(name: str, body: Pause):
        try:
            jobs.pause(name, body.paused)
        except LookupError as e:
            raise HTTPException(404, "no such job") from e

    @api.get("/jobs", response_class=HTMLResponse)
    def jobs_page():
        return page_html("mimir-tv")

    return api
