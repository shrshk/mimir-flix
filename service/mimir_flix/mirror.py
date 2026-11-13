"""The whole catalogue in Jellyfin: every movie and show a provider lists, as .strm files in its libraries, so
every Jellyfin app (Swiftfin, the TV app, the web) browses and searches all of it without mimir-flix's own pages.

Movies come straight from the catalogue's list (Catalog.everything). A show's episodes need its pages read, so
shows are written a batch per run, those never written first, then the ones written longest ago; a show the
provider fails to list waits for a later run. Followed shows (added by hand) keep their hourly check in strm.py and
are not written again here. Names follow strm.py, so a mirrored show and an added one share one folder.

A mirrored show keeps only its latest EPISODES_KEPT episodes (the catalogue's long-running shows would otherwise
put ~200,000 episodes in Jellyfin); a show favourited in Jellyfin becomes followed (follow_favourites), so it gets
every episode and the hourly check. Movies with the same title get numbered folders.

Each title also gets Jellyfin's own metadata files from what the catalogue says about it (write_details): a
movie.nfo or tvshow.nfo (plot, its language and kind as genres, a TV channel as studio, year) and poster.jpg, so
Jellyfin apps can list titles by language or channel even where online metadata finds nothing.

What was written is kept in .mirrored.json in each root ({link: {"name", "at"}}); nothing is removed in this
version when a title leaves the catalogue."""

import json
import re
import time
from xml.sax.saxutils import escape
from pathlib import Path
from urllib.parse import quote

import httpx

from mimir_flix import badges
from mimir_flix.catalog import Catalog, CatalogError
from mimir_flix.strm import follow, followed, safe, write_movie, write_show

MIRRORED = ".mirrored.json"
SHOWS_PER_RUN = 100  # page reads stay polite: the first pass over ~2,000 shows takes about a day
EPISODES_KEPT = 30  # a mirrored show's latest episodes; favourite it in Jellyfin for all of them
REWRITE_AFTER = 7 * 24 * 3600
POSTERS_PER_RUN = 1200  # poster downloads a run, about 20 a minute (the first pass over ~30,000 titles takes a day)
POSTER_MAX = 3_000_000
YEAR = re.compile(
    r"^(.*?)\s*\((\d{4})\)\s*$"
)  # a show's episodes are read again weekly (followed shows: hourly, elsewhere)


def mirrored(root: Path) -> dict[str, dict]:
    f = root / MIRRORED
    return json.loads(f.read_text()) if f.exists() else {}


def save(root: Path, done: dict[str, dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (MIRRORED + ".tmp")
    tmp.write_text(json.dumps(done, indent=1, sort_keys=True))
    tmp.replace(root / MIRRORED)


def folder_name(title: str, url: str, taken: dict[str, str]) -> str:
    """The show's folder: its title, or the title with a number when another show already has that name (macOS
    names ignore case). taken: folder name (lower case) -> the show's link."""
    name, n = safe(title), 2
    while taken.get(name.lower(), url) != url:
        name, n = safe(f"{title} ({n})"), n + 1
    return name


def mirror(
    cat: Catalog,
    pid: str,
    shows_root: Path,
    movies_root: Path,
    resolver: str,
    per_run: int = SHOWS_PER_RUN,
    now: float | None = None,
    http: httpx.Client | None = None,
) -> dict[str, int]:
    """One run: every movie not written yet, then up to per_run shows, then details for every written title (a
    batch of posters a run). Returns counts of what was written."""
    now = time.time() if now is None else now
    listed = cat.everything()
    out = {"movies": 0, "shows": 0, "episodes": 0, "failed": 0, "details": 0, "posters": 0}

    movies, taken_movies, changed = mirrored(movies_root), {}, False
    for m in (x for x in listed if x["kind"] == "movie"):
        rec = movies.get(m["url"])
        if rec and taken_movies.get(rec["name"].lower(), m["url"]) == m["url"] and points_at(movies_root, rec, m):
            taken_movies[rec["name"].lower()] = m["url"]
            continue
        name = folder_name(m["title"], m["url"], taken_movies)  # new, or its folder was another movie's
        write_movie(pid, m["url"], name, movies_root, resolver, m.get("page", ""))
        taken_movies[name.lower()] = m["url"]
        movies[m["url"]] = {"name": name, "at": now}
        out["movies"] += 1
        changed = True
    if changed:
        save(movies_root, movies)

    shows = mirrored(shows_root)
    by_hand = {url: name for name, url in followed(shows_root).items()}
    taken = {name.lower(): url for url, name in by_hand.items()} | {s["name"].lower(): url for url, s in shows.items()}
    due = [
        s
        for s in listed
        if s["kind"] == "show"
        and s["url"] not in by_hand
        and (s["url"] not in shows or now - shows[s["url"]]["at"] > REWRITE_AFTER)
    ]
    due.sort(key=lambda s: shows.get(s["url"], {}).get("at", 0))  # never written first, then the oldest
    for s in due[:per_run]:
        name = shows[s["url"]]["name"] if s["url"] in shows else folder_name(s["title"], s["url"], taken)
        try:
            files = write_show(cat, pid, s["url"], name, shows_root, resolver)
        except CatalogError:
            out["failed"] += 1
            continue
        taken[name.lower()] = s["url"]
        shows[s["url"]] = {"name": name, "at": now}
        out["shows"] += 1
        out["episodes"] += len(files) - trim(shows_root / safe(name), EPISODES_KEPT)
        save(shows_root, shows)  # after each show, so a stop part-way loses nothing

    names = {u: r["name"] for u, r in shows.items()} | by_hand
    dubbed = in_several_languages(listed)
    budget = [POSTERS_PER_RUN]
    own = http is None  # a client passed in stays open for its owner
    client = http or httpx.Client(timeout=20, follow_redirects=True)
    try:
        for x in listed:
            if x["kind"] == "movie" and x["url"] in movies:
                folder, tag = movies_root / movies[x["url"]]["name"], "movie"
            elif x["kind"] == "show" and x["url"] in names and (shows_root / names[x["url"]]).is_dir():
                folder, tag = shows_root / names[x["url"]], "tvshow"
            else:
                continue
            several = (x["kind"], base_title(x["title"])[0].lower()) in dubbed
            labels = badges.for_language(x.get("language"))
            changed, fetched = write_details(folder, tag, x, cat, client, budget, several, labels)
            out["details"] += changed
            out["posters"] += fetched
    finally:
        if own:
            client.close()
    return out


def base_title(title: str) -> tuple[str, str | None]:
    """A title without its "(year)", and the year."""
    m = YEAR.match(title)
    return (m.group(1), m.group(2)) if m else (title, None)


def in_several_languages(listed: list[dict]) -> set[tuple[str, str]]:
    """(kind, title in lower case) of titles the catalogue has in more than one language (a film and its dubs):
    their names in Jellyfin carry the language, so search results tell them apart."""
    seen: dict[tuple[str, str], set[str]] = {}
    for x in listed:
        if x.get("language"):
            seen.setdefault((x["kind"], base_title(x["title"])[0].lower()), set()).add(x["language"])
    return {k for k, langs in seen.items() if len(langs) > 1}


def nfo(tag: str, item: dict, with_language: bool = False) -> str:
    """Jellyfin's metadata file for a movie or a show from what the catalogue says: its language and kind as
    genres (and tags), each kind also as "<language> · <kind>" (one tag a home row can ask for), a TV channel as
    studio. with_language: the name carries the language ("Jailer 2 · Telugu"). Online metadata still fills what
    this leaves out."""
    title, year = base_title(item["title"])
    if with_language and item.get("language"):
        title = f"{title} · {item['language']}"
    lines = [f"  <title>{escape(title)}</title>"]
    if year:
        lines.append(f"  <year>{year}</year>")
    if item.get("plot"):
        lines.append(f"  <plot>{escape(item['plot'])}</plot>")
    for g in [item.get("language"), *item.get("genres", [])]:
        if g:
            lines += [f"  <genre>{escape(g)}</genre>", f"  <tag>{escape(g)}</tag>"]
    for g in item.get("genres", []) if item.get("language") else []:
        lines.append(f"  <tag>{escape(item['language'])} · {escape(g)}</tag>")
    if item.get("studio"):
        lines.append(f"  <studio>{escape(item['studio'])}</studio>")
    body = "\n".join(lines)
    return f'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<{tag}>\n{body}\n</{tag}>\n'


def write_details(
    folder: Path,
    tag: str,
    item: dict,
    cat: Catalog,
    http: httpx.Client,
    budget: list[int],
    with_language: bool = False,
    labels: list[tuple[str, str]] | None = None,
) -> tuple[bool, bool]:
    """The title's .nfo (rewritten only when it changes, as Jellyfin reads a changed file again) and, while the
    run's poster budget lasts, its poster from the catalogue (http or https, an image, at most POSTER_MAX bytes),
    with labels drawn on it (badges.py). Returns (nfo changed or poster drawn again, poster fetched)."""
    if not folder.is_dir():
        return False, False
    f, text = folder / f"{tag}.nfo", nfo(tag, item, with_language)
    changed = not f.exists() or f.read_text() != text
    if changed:
        f.write_text(text)
    poster, url, fetched = folder / "poster.jpg", item.get("thumb") or "", False
    have = poster.exists() or (folder / badges.ORIGINAL).exists()
    if url.startswith(("http://", "https://")) and not have and budget[0] > 0:
        budget[0] -= 1
        try:
            r = http.get(url)
            if (
                r.status_code == 200
                and r.headers.get("content-type", "").startswith("image/")
                and len(r.content) <= POSTER_MAX
            ):
                poster.write_bytes(r.content)
                fetched = True
        except httpx.HTTPError:
            pass  # no poster this time; tried again next run
    if labels is not None and badges.apply(folder, labels):
        changed = True
    return changed, fetched


def points_at(root: Path, rec: dict, movie: dict) -> bool:
    """Whether the movie's recorded file still plays this movie (an older version gave same-titled movies one
    folder, the last written winning)."""
    f = root / rec["name"] / f"{rec['name']}.strm"
    return f.exists() and quote(movie["url"], safe="") in f.read_text()


EPISODE = re.compile(r" S(\d+)E(\d+)(?: - .*)?\.strm$")


def trim(show_dir: Path, keep: int) -> int:
    """Leaves a mirrored show's latest keep episodes (by season, then episode); returns how many were removed. These
    are mirror.py's own .strm files, never anything else."""
    eps = sorted((tuple(map(int, m.groups())), f) for f in show_dir.rglob("*.strm") if (m := EPISODE.search(f.name)))
    gone = [f for _, f in eps[:-keep]] if len(eps) > keep else []
    for f in gone:
        f.unlink()
    for d in show_dir.iterdir() if show_dir.exists() else []:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return len(gone)


def follow_favourites(shows_root: Path, favourite_paths: list[str]) -> list[str]:
    """Mirrored shows favourited in Jellyfin become followed: every episode, checked hourly (strm.refresh_shows).
    Returns their names."""
    by_name = {rec["name"]: url for url, rec in mirrored(shows_root).items()}
    already = followed(shows_root)
    out = []
    for p in map(Path, favourite_paths):
        if p.parent == shows_root and p.name in by_name and p.name not in already:
            follow(shows_root, p.name, by_name[p.name])
            out.append(p.name)
    return out


def left(cat: Catalog, shows_root: Path, now: float | None = None) -> int:
    """How many listed shows are still waiting for their first write (for the job's summary)."""
    shows, by_hand = mirrored(shows_root), set(followed(shows_root).values())
    return sum(1 for s in cat.everything() if s["kind"] == "show" and s["url"] not in shows and s["url"] not in by_hand)
