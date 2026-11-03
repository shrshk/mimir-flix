"""mimir-flix's scheduled jobs, each run recorded with jobkit in mimir-flix's own database; the jobs page reads
GET /api/jobs."""

import sqlite3
from pathlib import Path

import httpx

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from jobkit import Jobs, page_html

from mimir_flix.catalog import CatalogError, Catalogs, Job
from mimir_flix.catalog_routes import libraries, roots
from mimir_flix.jellyfin import Jellyfin
from mimir_flix.strm import refresh_shows
from mimir_flix.watchlist import Watchlist
from mimir_flix.wizard import allowed

NEW_EPISODES = "new-episodes"


def define(jobs: Jobs) -> None:
    jobs.define(NEW_EPISODES, "New episodes: added shows into Jellyfin, My list shows marked", "hourly")


def new_episodes(
    jobs: Jobs, catalogs: Catalogs, jf: Jellyfin, media_root: Path, resolver: str, watchlist: Watchlist | None = None
) -> int:
    """New episodes of every provider's followed shows into their folders, then a scan so Jellyfin lists them; shows
    on My list get a count of their new episodes (nothing goes into Jellyfin for those)."""
    with jobs.run(NEW_EPISODES) as run:
        listed = watchlist.check(catalogs) if watchlist else 0
        found = 0
        for entry, cat in catalogs.all():
            new = refresh_shows(cat, entry["id"], roots(media_root, entry["id"])["shows"], resolver)
            if new:
                lib = next((x for x in jf.libraries() if x["Name"] == libraries(entry)["shows"]), None)
                if lib:
                    jf.scan(lib.get("ItemId"))
            found += new
        run.summary = f"{found} new episode{'s' if found != 1 else ''}" + (f", {listed} on My list" if listed else "")
        return found


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
        return {"app": "mimir-flix", "jobs": jobs.report()}

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
        return page_html("mimir-flix")

    return api
