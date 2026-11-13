"""Jellyfin's library API, the parts the Library page needs. Uses an API key in the standard Authorization header
(Jellyfin 10.11+ rejects the old X-Emby-Authorization header)."""

import time

import httpx


class JellyfinError(Exception):
    pass


class JellyfinMissing(JellyfinError):
    """Jellyfin has no such item (it was removed)."""


class Jellyfin:
    def __init__(self, url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=url,
            headers={
                "Authorization": f'MediaBrowser Client="mimir-flix", Device="mimir-flix", DeviceId="mimir-flix-air", '
                f'Version="0.1", Token="{api_key}"'
            },
            timeout=30,
            transport=transport,
        )
        self._user = ""

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        try:
            r = self.http.request(method, path, params=params, json=body)
        except httpx.HTTPError as e:
            raise JellyfinError(f"Jellyfin not reachable: {e}") from e
        if r.status_code >= 400:
            raise JellyfinError(f"Jellyfin said {r.status_code}: {r.text[:200]}")
        return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else None

    def libraries(self) -> list[dict]:
        return self._call("GET", "/Library/VirtualFolders")

    def create_library(self, name: str, kind: str, paths: list[str]) -> None:
        options = {"PathInfos": [{"Path": p} for p in paths]}
        params = {"name": name, "collectionType": kind, "refreshLibrary": "true"}
        self._call("POST", "/Library/VirtualFolders", params, {"LibraryOptions": options})

    def delete_library(self, name: str) -> None:
        self._call("DELETE", "/Library/VirtualFolders", {"name": name, "refreshLibrary": "true"})

    def rename_library(self, name: str, new_name: str) -> None:
        self._call(
            "POST", "/Library/VirtualFolders/Name", {"name": name, "newName": new_name, "refreshLibrary": "true"}
        )

    def add_path(self, library: str, path: str) -> None:
        body = {"Name": library, "PathInfo": {"Path": path}}
        self._call("POST", "/Library/VirtualFolders/Paths", {"refreshLibrary": "true"}, body)

    def remove_path(self, library: str, path: str) -> None:
        self._call("DELETE", "/Library/VirtualFolders/Paths", {"name": library, "path": path, "refreshLibrary": "true"})

    def scan(self, item_id: str | None = None) -> None:
        if item_id:
            params = {"Recursive": "true", "MetadataRefreshMode": "Default", "ImageRefreshMode": "Default"}
            self._call("POST", f"/Items/{item_id}/Refresh", params)
        else:
            self._call("POST", "/Library/Refresh")

    def scan_task(self) -> dict | None:
        """The library scan, when one is running: its progress in percent."""
        for t in self._call("GET", "/ScheduledTasks") or []:
            if t.get("Key") == "RefreshLibrary" or t.get("Name") == "Scan Media Library":
                if t.get("State") == "Running":
                    return {"progress": round(t.get("CurrentProgressPercentage") or 0, 1)}
        return None

    # Browsing, and the fallback player (the page never calls Jellyfin's API itself).
    FIELDS = "Overview,ProductionYear,RunTimeTicks,MediaSources,UserData,ChildCount"

    def views(self) -> list[dict]:
        return (self._call("GET", "/Library/MediaFolders") or {}).get("Items", [])

    def children(self, parent_id: str) -> list[dict]:
        params = {
            "ParentId": parent_id,
            "SortBy": "IsFolder,SortName",
            "SortOrder": "Descending,Ascending",
            "Fields": self.FIELDS,
            "Limit": "500",
        }
        return (self._call("GET", "/Items", params) or {}).get("Items", [])

    def search(self, q: str, limit: int = 40) -> list[dict]:
        params = {
            "SearchTerm": q,
            "Recursive": "true",
            "Limit": str(limit),
            "Fields": self.FIELDS,
            "IncludeItemTypes": "Movie,Series,Episode,Video,MusicVideo,Folder,TvChannel",  # channels: IPTV
        }
        return (self._call("GET", "/Items", params) or {}).get("Items", [])

    def item(self, item_id: str) -> dict:
        items = (self._call("GET", "/Items", {"Ids": item_id, "Fields": self.FIELDS}) or {}).get("Items", [])
        if not items:
            raise JellyfinMissing("no such item")
        return items[0]

    DETAIL_FIELDS = FIELDS + ",Genres,People,OfficialRating,CommunityRating"

    def details(self, item_id: str) -> dict:
        """One title with what its page shows: genres, cast and crew, ratings, and where it was left."""
        params = {"Ids": item_id, "Fields": self.DETAIL_FIELDS, "userId": self.user()}
        items = (self._call("GET", "/Items", params) or {}).get("Items", [])
        if not items:
            raise JellyfinMissing("no such item")
        return items[0]

    def similar(self, item_id: str, limit: int = 12) -> list[dict]:
        """The library's titles like this one, by Jellyfin's own measure (genres, people, tags)."""
        params = {"userId": self.user(), "Limit": str(limit), "Fields": self.FIELDS}
        return (self._call("GET", f"/Items/{item_id}/Similar", params) or {}).get("Items", [])

    def raw(self, path: str, params: dict, headers: dict) -> httpx.Response:
        """A streamed passthrough (video bytes, HLS playlists, images); the caller closes it."""
        try:
            req = self.http.build_request("GET", path, params=params, headers=headers)
            return self.http.send(req, stream=True)
        except httpx.HTTPError as e:
            raise JellyfinError(f"Jellyfin not reachable: {e}") from e

    # Jellyfin plays everything: on the TV through its TV app (which keeps track of what was watched).
    def user(self) -> str:
        """The account the TV app signs in with: the first administrator (this Jellyfin has one user)."""
        if not self._user:
            users = self._call("GET", "/Users") or []
            admin = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), None)
            if not admin:
                raise JellyfinError("Jellyfin has no administrator")
            self._user = admin["Id"]
        return self._user

    def resume(self, limit: int = 20) -> list[dict]:
        """Started and not finished (Continue watching), most recent first."""
        params = {"userId": self.user(), "Limit": str(limit), "Fields": self.FIELDS, "MediaTypes": "Video"}
        return (self._call("GET", "/UserItems/Resume", params) or {}).get("Items", [])

    def next_up(self, limit: int = 20) -> list[dict]:
        """The next episode of each show being watched."""
        params = {"userId": self.user(), "Limit": str(limit), "Fields": self.FIELDS}
        return (self._call("GET", "/Shows/NextUp", params) or {}).get("Items", [])

    def watched_titles(self) -> set[str]:
        """Movies played, and shows with a played episode, by lower-case title (recommendations leave them out)."""
        params = {
            "userId": self.user(),
            "Recursive": "true",
            "Filters": "IsPlayed",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": "SeriesName",
            "Limit": "2000",
        }
        items = (self._call("GET", "/Items", params) or {}).get("Items", [])
        return {(i.get("SeriesName") or i.get("Name") or "").lower() for i in items} - {""}

    # Live TV (IPTV providers, M2): Jellyfin reads mimir-flix's local playlist and guide.
    def add_tuner(self, url: str, name: str) -> str:
        body = {"Type": "m3u", "Url": url, "FriendlyName": name, "AllowHWTranscoding": True, "TunerCount": 0}
        return (self._call("POST", "/LiveTv/TunerHosts", body=body) or {}).get("Id", "")

    def remove_tuner(self, tuner_id: str) -> None:
        self._call("DELETE", "/LiveTv/TunerHosts", {"id": tuner_id})

    def add_listings(self, url: str) -> str:
        body = {"Type": "xmltv", "Path": url, "EnableAllTuners": True}
        return (self._call("POST", "/LiveTv/ListingProviders", body=body) or {}).get("Id", "")

    def remove_listings(self, listings_id: str) -> None:
        self._call("DELETE", "/LiveTv/ListingProviders", {"id": listings_id})

    def refresh_guide(self) -> None:
        """Runs Jellyfin's guide refresh now (it also finds new channels)."""
        for t in self._call("GET", "/ScheduledTasks") or []:
            if t.get("Key") == "RefreshGuide":
                self._call("POST", f"/ScheduledTasks/Running/{t['Id']}")
                return

    TASTE_FIELDS = "Genres,People,ProviderIds,ProductionYear,UserData,SeriesName"

    def played(self, limit: int = 200) -> list[dict]:
        """Movies and shows watched, most recent first: movies played, and shows with an episode played (with how
        many). Movie and show libraries only: home videos are Video items, never asked for. Each with its genres,
        people, TMDB id, last played date and whether it is a favourite."""
        base = {"userId": self.user(), "Recursive": "true", "Fields": self.TASTE_FIELDS}
        movies = (
            self._call(
                "GET",
                "/Items",
                base
                | {
                    "Filters": "IsPlayed",
                    "IncludeItemTypes": "Movie",
                    "SortBy": "DatePlayed",
                    "SortOrder": "Descending",
                    "Limit": str(limit),
                },
            )
            or {}
        ).get("Items", [])
        episodes = (
            self._call(
                "GET",
                "/Items",
                base
                | {
                    "Filters": "IsPlayed",
                    "IncludeItemTypes": "Episode",
                    "SortBy": "DatePlayed",
                    "SortOrder": "Descending",
                    "Limit": "2000",
                    "Fields": "SeriesId,UserData",
                },
            )
            or {}
        ).get("Items", [])
        shows: dict[str, dict] = {}
        for e in episodes:
            sid = e.get("SeriesId")
            if sid:
                last = (e.get("UserData") or {}).get("LastPlayedDate") or ""
                seen = shows.setdefault(sid, {"episodes": 0, "last": last})
                seen["episodes"] += 1
                seen["last"] = max(seen["last"], last)
        series = []
        for at in range(0, len(shows), 100):
            ids = ",".join(list(shows)[at : at + 100])
            series += (self._call("GET", "/Items", base | {"Ids": ids}) or {}).get("Items", [])
        favourites = (
            self._call("GET", "/Items", base | {"Filters": "IsFavorite", "IncludeItemTypes": "Movie,Series"}) or {}
        ).get("Items", [])

        def one(i: dict, kind: str, episodes: int = 0, last: str = "") -> dict:
            people = i.get("People") or []
            tmdb = (i.get("ProviderIds") or {}).get("Tmdb") or ""
            return {
                "title": i.get("Name") or "",
                "kind": kind,
                "year": i.get("ProductionYear") or 0,
                "tmdb": f"{'movie' if kind == 'movie' else 'tv'}/{tmdb}" if tmdb else "",
                "genres": i.get("Genres") or [],
                "directors": [p["Name"] for p in people if p.get("Type") == "Director"][:2],
                "cast": [p["Name"] for p in people if p.get("Type") == "Actor"][:5],
                "favourite": bool((i.get("UserData") or {}).get("IsFavorite")),
                "episodes": episodes,
                "last_played": (last or (i.get("UserData") or {}).get("LastPlayedDate") or "")[:10],
            }

        out = [one(m, "movie") for m in movies]
        out += [one(s, "show", shows[s["Id"]]["episodes"], shows[s["Id"]]["last"]) for s in series if s["Id"] in shows]
        known = {(o["title"], o["kind"]) for o in out}
        for f in favourites:  # a favourite not played yet still says what is liked
            kind = "movie" if f.get("Type") == "Movie" else "show"
            if (f.get("Name"), kind) not in known:
                out.append(one(f, kind))
        return sorted(out, key=lambda o: o["last_played"], reverse=True)

    # What Play here watched (P1). Jellyfin's session reports show it as playing, but an API key has no user, so
    # they save nothing: the position and played state are written to the user's data as well.
    def report(self, event: str, body: dict) -> None:
        """event: "" (started), "Progress" or "Stopped"."""
        self._call("POST", "/Sessions/Playing" + (f"/{event}" if event else ""), body=body)

    def user_data(self, item_id: str) -> dict:
        return self._call("GET", f"/UserItems/{item_id}/UserData", {"userId": self.user()}) or {}

    def save_position(self, item_id: str, ticks: int) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        body = {"PlaybackPositionTicks": ticks, "LastPlayedDate": now}
        self._call("POST", f"/UserItems/{item_id}/UserData", {"userId": self.user()}, body)

    def mark_played(self, item_id: str) -> None:
        """Played, its play count up and its position back to the start (as Jellyfin does at the end)."""
        self._call("POST", f"/UserPlayedItems/{item_id}", {"userId": self.user()})

    def next_episode(self, item: dict) -> dict | None:
        """The episode after this one in its show, or None (the last one, or not an episode)."""
        if item.get("Type") != "Episode" or not item.get("SeriesId"):
            return None
        params = {"userId": self.user(), "startItemId": item["Id"], "limit": "2", "fields": self.FIELDS}
        after = (self._call("GET", f"/Shows/{item['SeriesId']}/Episodes", params) or {}).get("Items", [])
        return next((e for e in after if e.get("Id") != item["Id"]), None)

    # My list's titles in the home library are the user's Jellyfin favourites (P3).
    LISTED = "Movie,Series,Season,Episode,Video,MusicVideo,Folder,BoxSet"

    def favourites(self) -> list[dict]:
        params = {
            "userId": self.user(),
            "Recursive": "true",
            "Filters": "IsFavorite",
            "IncludeItemTypes": self.LISTED,
            "Fields": self.FIELDS,
            "SortBy": "SortName",
        }
        return (self._call("GET", "/Items", params) or {}).get("Items", [])

    def favourite_show_paths(self) -> list[str]:
        """Where each favourited show's folder is (mirror.py follows the mirrored ones)."""
        params = {
            "userId": self.user(),
            "Recursive": "true",
            "Filters": "IsFavorite",
            "IncludeItemTypes": "Series",
            "Fields": "Path",
        }
        return [i["Path"] for i in (self._call("GET", "/Items", params) or {}).get("Items", []) if i.get("Path")]

    def set_favourite(self, item_id: str, on: bool) -> None:
        self._call("POST" if on else "DELETE", f"/UserFavoriteItems/{item_id}", {"userId": self.user()})

    def app_ids(self) -> dict:
        """What a Swiftfin link needs: swiftfin://<server>/<user>/item/<id> (the iPhone app opens that item)."""
        info = self._call("GET", "/System/Info/Public") or {}
        return {"server": info.get("Id", ""), "user": self.user()}

    def tv_session(self) -> dict | None:
        """The Jellyfin TV app (Wholphin or Jellyfin's own), when it is open and can take a play command."""
        params = {"ControllableByUserId": self.user(), "ActiveWithinSeconds": "960"}
        tvs = [
            x
            for x in self._call("GET", "/Sessions", params) or []
            if ("TV" in (x.get("Client") or "") or x.get("Client") == "Wholphin")
            and x.get("SupportsRemoteControl")
            and "Video" in (x.get("PlayableMediaTypes") or [])
        ]
        return max(tvs, key=lambda x: x.get("LastActivityDate") or "", default=None)

    def phone_session(self, since: str) -> dict | None:
        """Swiftfin (Jellyfin's iPhone app) active since `since` (UTC, ISO): the phone that just opened it. Swiftfin
        1.6+ takes play commands over its socket (supportsMediaControl, PlayNow)."""
        params = {"ControllableByUserId": self.user(), "ActiveWithinSeconds": "120"}
        apps = [
            x
            for x in self._call("GET", "/Sessions", params) or []
            if "Swiftfin" in (x.get("Client") or "")
            and x.get("SupportsRemoteControl")
            and (x.get("LastActivityDate") or "") >= since
        ]
        return max(apps, key=lambda x: x.get("LastActivityDate") or "", default=None)

    def play_on_phone(self, item_id: str, wait: float = 0.5, tries: int = 30) -> None:
        """Starts it in Swiftfin, which the page opens at the item at the same moment (Swiftfin's own links only
        open an item's page): waits for the app to come up, then sends it PlayNow, as for the TV app."""
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 2))
        for _ in range(tries):
            if phone := self.phone_session(since):
                self._call("POST", f"/Sessions/{phone['Id']}/Playing", {"playCommand": "PlayNow", "itemIds": item_id})
                return
            time.sleep(wait)
        raise JellyfinError("Swiftfin did not open")

    def play_on_tv(
        self, item_id: str, open_app=None, wait: float = 1, tries: int = 30, settle: float = 3, play_app=None
    ) -> None:
        """Starts it in the Jellyfin TV app. play_app (if given) plays it in Wholphin by intent and returns True, or
        False when Wholphin is not there. Otherwise open_app (if given) brings the app to the front unless it already is
        (it returns False then): an app left in the background still has a session but cannot come forward for a
        play command. After opening, this waits for the app to connect, then `settle` seconds more: the app connects
        before its home screen has loaded, and a play command then shows "no playable items" first."""
        if play_app:  # Wholphin: started by its own intent, from where it was left
            ticks = (self.user_data(item_id) or {}).get("PlaybackPositionTicks") or 0
            if play_app(item_id, ticks // 10_000):
                return
        tv = self.tv_session()
        if open_app and open_app() is not False:  # it was closed or behind another app: opened now
            for _ in range(tries):
                time.sleep(wait)
                if tv := self.tv_session():
                    time.sleep(settle)
                    break
        if not tv:
            raise JellyfinError("Open Jellyfin on the TV first")
        if tv.get("NowPlayingItem"):  # the TV app takes "play" during playback as "stop": stop it first
            self._call("POST", f"/Sessions/{tv['Id']}/Playing/Stop")
            for _ in range(tries):
                time.sleep(wait)
                if not (tv := self.tv_session() or tv).get("NowPlayingItem"):
                    break
        params = {"playCommand": "PlayNow", "itemIds": item_id}
        self._call("POST", f"/Sessions/{tv['Id']}/Playing", params)

    def library_items(self, library: str) -> list[dict]:
        """A library's movies, shows, episodes and videos with their paths ([] when there is no such library)."""
        lib = next((x for x in self.libraries() or [] if x["Name"] == library), None)
        if not (lib and lib.get("ItemId")):
            return []
        params = {
            "ParentId": lib["ItemId"],
            "Recursive": "true",
            "Fields": "Path",
            "IncludeItemTypes": "Movie,Series,Episode,Video",
        }
        return (self._call("GET", "/Items", params) or {}).get("Items", [])

    def recent_episodes(self, days: int = 7, limit: int = 60) -> list[dict]:
        """Episodes added in the last `days`, newest first (providers' followed shows get theirs hourly)."""
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Episode",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Limit": str(limit),
            "Fields": "DateCreated",
        }
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))
        items = (self._call("GET", "/Items", params) or {}).get("Items", [])
        return [i for i in items if (i.get("DateCreated") or "")[:19] >= since]

    def find(self, library: str, path: str, wait: float = 2, tries: int = 30) -> str:
        """The id Jellyfin gives a file or folder once its scan reaches it."""
        for _ in range(tries):
            for i in self.library_items(library):
                if i.get("Path") == path:
                    return i["Id"]
            time.sleep(wait)
        raise JellyfinError("Jellyfin is still adding it; try again in a minute")

    def count(self, library_id: str) -> int:
        params = {"ParentId": library_id, "Recursive": "true", "Limit": "0", "MediaTypes": "Video,Photo,Audio"}
        return (self._call("GET", "/Items", params) or {}).get("TotalRecordCount", 0)
