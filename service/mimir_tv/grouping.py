"""The catalogue grouped like mimir-tv's provider page: one Jellyfin collection per language ("Telugu"), holding
one per kind ("Telugu TV serials", "Telugu web series", "Telugu movies"). Jellyfin apps list collections in their
side menu (Wholphin: Collections), so the catalogue browses by language, then kind, in any of them.

Membership comes from the tags mirror.py writes ("<language> · <kind>", and the language on movies); each run adds
what is new and takes out what left. Languages and their order come from Settings' library_languages. Only
collections with these names are touched."""

from mimir_tv.jellyfin import Jellyfin, JellyfinError

KINDS = [("TV serial", "TV serials"), ("Web series", "web series")]
BATCH = 50  # item ids per add or remove call (they go in the URL)


def _ids(jf: Jellyfin, **filters) -> set[str]:
    params = {"Recursive": "true", "userId": jf.user(), "Fields": "", "EnableImages": "false"} | filters
    return {i["Id"] for i in (jf._call("GET", "/Items", params) or {}).get("Items", [])}


def _boxsets(jf: Jellyfin) -> dict[str, str]:
    found = jf._call("GET", "/Items", {"Recursive": "true", "IncludeItemTypes": "BoxSet", "userId": jf.user()})
    return {i["Name"]: i["Id"] for i in (found or {}).get("Items", [])}


def _sync(jf: Jellyfin, have: dict[str, str], name: str, want: set[str]) -> tuple[str, int]:
    """The collection called name holding exactly want (made when missing); returns its id and how many changed."""
    if name not in have:  # made empty, then filled below, so a gone title cannot stop it being made
        have[name] = jf._call("POST", "/Collections", {"Name": name})["Id"]
    cid = have[name]
    now = _ids(jf, ParentId=cid)
    add, drop = sorted(want - now), sorted(now - want)
    added = 0
    for i in range(0, len(add), BATCH):
        batch = add[i : i + BATCH]
        try:
            jf._call("POST", f"/Collections/{cid}/Items", {"Ids": ",".join(batch)})
            added += len(batch)
        except JellyfinError:
            # Jellyfin refuses the whole batch when one title is gone (a scan removing titles the mirror trimmed
            # while this runs): one at a time, leaving out the gone ones; the next run sees the scan's result.
            for one in batch:
                try:
                    jf._call("POST", f"/Collections/{cid}/Items", {"Ids": one})
                    added += 1
                except JellyfinError:
                    pass
    for i in range(0, len(drop), BATCH):
        jf._call("DELETE", f"/Collections/{cid}/Items", {"Ids": ",".join(drop[i : i + BATCH])})
    return cid, added + len(drop)


def _sort_as(jf: Jellyfin, cid: str, key: str) -> None:
    """The collection's sort name, so languages list in the house's order rather than alphabetically."""
    item = jf._call("GET", f"/Items/{cid}", {"userId": jf.user()}) or {}
    if item.get("ForcedSortName") != key:
        jf._call("POST", f"/Items/{cid}", None, item | {"ForcedSortName": key})


def group(jf: Jellyfin, shows_lib: str, movies_lib: str, languages: list[str]) -> str:
    """One run; a line for the jobs page."""
    have, changed, made = _boxsets(jf), 0, []
    for n, lang in enumerate(languages, 1):
        parts: dict[str, set[str]] = {}
        shows = _ids(jf, ParentId=shows_lib, IncludeItemTypes="Series", Tags=lang)
        kinds = {
            label: _ids(jf, ParentId=shows_lib, IncludeItemTypes="Series", Tags=f"{lang} · {k}") for k, label in KINDS
        }
        for label, ids in kinds.items():
            if ids:
                parts[f"{lang} {label}"] = ids
        rest = shows - set().union(*kinds.values())  # shows without a kind (English shows)
        if rest:
            parts[f"{lang} shows" if not any(kinds.values()) else f"{lang} other shows"] = rest
        movies = _ids(jf, ParentId=movies_lib, IncludeItemTypes="Movie", Tags=lang)
        if movies:
            parts[f"{lang} movies"] = movies
        if not parts:
            continue
        inner = {}
        for m, (name, ids) in enumerate(parts.items(), 1):
            inner[name], k = _sync(jf, have, name, ids)
            changed += k
            _sort_as(jf, inner[name], f"{n:02d} {lang} {m}")  # listed under their language
        outer, k = _sync(jf, have, lang, set(inner.values()))
        changed += k
        _sort_as(jf, outer, f"{n:02d} {lang}")
        made.append(lang)
    return f"{', '.join(made) or 'no languages'}: {changed} changes"
