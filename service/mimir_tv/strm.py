"""A provider's shows as Jellyfin TV shows: one .strm file per episode, pointing at the resolver (which signs the
link when Jellyfin plays it). Laid out the way Jellyfin names shows: <Show>/Season NN/<Show> SNNENN - <title>.strm.

A .strm line is <resolver>/play?p=<provider id>&path=<link>; lines written before providers had ids have no p= and
are left as they are (the resolver reads them as the first provider's)."""

import json
import re
from pathlib import Path
from urllib.parse import quote

from mimir_tv.catalog import Catalog, CatalogError

FOLLOWED = ".followed.json"  # shows added to Jellyfin (title: the show's page), checked for new episodes
ADDED = ".added.json"  # movies added to Jellyfin by hand (folder names): the mirror's limit never removes them
LIST_FOR = 6 * 3600  # how old a show's listing may be when it is written


def number(text: str) -> int | None:
    """The season or episode number in a title ("Season 9", "Episode 2. …", "Ep 3")."""
    m = re.search(r"(?:episode|ep|season|s)\s*\.?\s*(\d+)", text, re.I) or re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def play_line(resolver: str, pid: str, url: str, page: str = "") -> str:
    extra = f"&page={quote(page, safe='')}" if page else ""
    return f"{resolver}/play?p={quote(pid, safe='')}&path={quote(url, safe='')}{extra}\n"


def keep(target: Path, line: str, pid: str) -> bool:
    """The file already says this (or said it before providers had ids): left alone, since Jellyfin forgets what
    it knows about a changed file, and a TV play landing then finds nothing to play."""
    if not target.exists():
        return False
    return target.read_text() in (line, line.replace(f"p={quote(pid, safe='')}&", "", 1))


def safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip().rstrip(".") or "Untitled"


def write_show(
    cat: Catalog, pid: str, show_url: str, title: str, root: Path, resolver: str, ttl: int = LIST_FOR
) -> dict[str, Path]:
    """Writes (or refreshes) a show's .strm files; returns each episode's file by its link."""
    show_dir = root / safe(title)
    items = cat.page(show_url, ttl=ttl)
    seasons = [i for i in items if i["kind"] == "folder"]
    groups = [(number(s["title"]) or n, cat.page(s["url"], ttl=ttl)) for n, s in enumerate(reversed(seasons), 1)]
    loose = [i for i in items if i["kind"] == "video"]
    if loose:
        groups.append((1 if not seasons else 0, loose))
    files = {}
    for season, episodes in groups:
        folder = show_dir / f"Season {season:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        # Listings are newest first; without a number in the title, the oldest is episode 1.
        for pos, ep in enumerate(reversed([e for e in episodes if e["kind"] == "video"]), 1):
            n = number(ep["title"]) or pos
            label = re.sub(r"^(episode|ep)\s*\.?\s*\d+\s*[.:-]?\s*", "", ep["title"], flags=re.I)
            name = f"{safe(title)} S{season:02d}E{n:02d}" + (f" - {safe(label)}" if label else "")
            target = folder / f"{name}.strm"
            line = play_line(resolver, pid, ep["url"])
            if not keep(target, line, pid):
                target.write_text(line)
            files[ep["url"]] = target
    return files


def followed(root: Path) -> dict[str, str]:
    f = root / FOLLOWED
    return json.loads(f.read_text()) if f.exists() else {}


def show_folder(root: Path, title: str, show_url: str, cat: Catalog) -> str:
    """The show's folder, which is also its name in Jellyfin: the title, unless another followed show already uses
    that name (the same show from another section); then the title with its section. Adding again reuses it."""
    shows = followed(root)
    for name, url in shows.items():
        if url == show_url:
            return name
    name = safe(title)
    taken = {n.lower() for n in shows}  # macOS folder names ignore case
    if name.lower() not in taken:
        return name
    label = cat.section_of(show_url)
    other = safe(f"{title} ({label})") if label else ""
    n = 2
    while not other or other.lower() in taken:  # no label, or that one is taken too
        other, n = safe(f"{title} ({n})"), n + 1
    return other


def added(root: Path) -> list[str]:
    f = root / ADDED
    return json.loads(f.read_text()) if f.exists() else []


def add(root: Path, name: str) -> None:
    names = sorted(set(added(root)) | {name})
    root.mkdir(parents=True, exist_ok=True)
    (root / ADDED).write_text(json.dumps(names, indent=1))


def follow(root: Path, title: str, show_url: str) -> None:
    shows = followed(root) | {title: show_url}
    root.mkdir(parents=True, exist_ok=True)
    (root / FOLLOWED).write_text(json.dumps(shows, indent=1, sort_keys=True))


def refresh_shows(cat: Catalog, pid: str, root: Path, resolver: str, ttl: int = 3000) -> int:
    """Writes episodes that appeared since a show was added; returns how many are new. A show the provider fails to
    list is skipped until the next round."""
    new = 0
    for title, url in followed(root).items():
        before = set((root / safe(title)).rglob("*.strm"))
        try:
            files = write_show(cat, pid, url, title, root, resolver, ttl)  # listings at most ttl seconds old
        except CatalogError:
            continue
        new += len(set(files.values()) - before)
    return new


def write_movie(pid: str, url: str, title: str, root: Path, resolver: str, page: str = "") -> Path:
    """<root>/<Title (Year)>/<Title (Year)>.strm, the layout Jellyfin expects for a movie. page: the list it came
    from (a live channel's link expires; the resolver finds the fresh one there)."""
    folder = root / safe(title)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{safe(title)}.strm"
    line = play_line(resolver, pid, url, page)
    if not keep(target, line, pid):
        target.write_text(line)
    return target
