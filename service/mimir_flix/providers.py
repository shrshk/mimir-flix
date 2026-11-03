"""Providers. Plugin providers (catalog.py) are kept here too: a name, a kind, and the login in the vault; their
pages are catalog_routes.py.

IPTV providers (M2): live channels from an M3U playlist, or an Xtream Codes server with a username and password,
played by Jellyfin's Live TV (Jellyfin plays everything; its apps on the phone and the TV show the channels and
the guide). mimir-flix only connects them:

- The login, or a playlist URL that carries one, is kept in the vault, never in settings.json or Jellyfin.
- Jellyfin is given mimir-flix's own copy of the playlist (/iptv/<id>/playlist.m3u) whose stream links point back at
  mimir-flix (/iptv/<id>/stream/<n>), which redirects to the provider's real link; the guide (XMLTV) the same way.
  Those addresses answer only this Mac (Jellyfin runs here), so the login never leaves it.
- Settings adds and removes providers; removing one takes its channels and guide out of Jellyfin too.
"""

import re
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from mimir_flix import settings
from mimir_flix.catalog import CatalogError, Catalogs
from mimir_flix.jellyfin import Jellyfin, JellyfinError
from mimir_flix.prereqs import from_this_mac
from mimir_flix.vault import Vault

PLAYLIST_TTL = 3600  # a provider's playlist is fetched again after an hour
GUIDE_ATTRS = re.compile(r'\s(?:url-tvg|x-tvg-url|tvg-url)="[^"]*"', re.I)  # can carry the login: Jellyfin gets ours


class NewProvider(BaseModel):
    model_config = ConfigDict(extra="allow")  # a plugin's own fields (catalog.py), checked against what it declares

    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(min_length=1, max_length=40)  # m3u, xtream, or an installed plugin's kind
    url: str = ""  # m3u: the playlist's address; a plugin: its login address, when it asks for one
    epg: str = ""  # m3u: the guide's (XMLTV) address, optional
    server: str = ""  # xtream: e.g. http://example.tv:8080
    user: str = ""
    password: str = ""


class ProviderError(Exception):
    pass


def upstream(kind: str, server: str, secret: dict) -> tuple[str, str]:
    """The provider's own playlist and guide addresses (with the login in them)."""
    if kind == "m3u":
        return secret["url"], secret.get("epg", "")
    q = urlencode({"username": secret["user"], "password": secret["password"]})
    base = server.rstrip("/")
    return f"{base}/get.php?{q}&type=m3u_plus&output=ts", f"{base}/xmltv.php?{q}"


def rewrite(playlist: str, base: str) -> tuple[str, list[str]]:
    """The playlist with every stream link replaced by ours, and the real links in order (the n of /stream/<n>)."""
    lines, links = [], []
    for line in playlist.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTM3U"):
            s = GUIDE_ATTRS.sub("", s)
        elif not s.startswith("#"):
            links.append(s)
            s = f"{base}/stream/{len(links) - 1}"
        lines.append(s)
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ProviderError("that is not an M3U playlist")
    return "\n".join(lines) + "\n", links


class Providers:
    def __init__(
        self,
        config: Path,
        vault: Vault,
        jf: Callable[[], Jellyfin | None],
        port: int,
        transport: httpx.BaseTransport | None = None,
        catalogs: Catalogs | None = None,
        base: str = "",
    ):
        """base: mimir-flix's address on the home network (what other players are given)."""
        self.config, self.vault, self.jf, self.port = config, vault, jf, port
        self.catalogs, self.base = catalogs, base
        self.http = httpx.Client(timeout=30, follow_redirects=True, transport=transport)
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[float, str, list[str]]] = {}  # id -> (fetched, our playlist, real links)

    def listed(self) -> list[dict]:
        return settings.load(self.config)["providers"]

    def find(self, pid: str) -> dict:
        found = next((p for p in self.listed() if p["id"] == pid), None)
        if found is None:
            raise ProviderError("no such provider")
        return found

    def local(self, pid: str) -> str:
        return f"http://127.0.0.1:{self.port}/iptv/{pid}"

    def addresses(self, pid: str) -> tuple[str, str]:
        p = self.find(pid)
        secret = self.vault.get(f"provider:{pid}")
        if not secret:
            raise ProviderError("its login is missing from the vault: add the provider again")
        return upstream(p["kind"], p.get("server", ""), secret)

    def playlist(self, pid: str, fresh: bool = False) -> tuple[str, list[str]]:
        hit = self.cache.get(pid)
        if hit and not fresh and hit[0] > time.time() - PLAYLIST_TTL:
            return hit[1], hit[2]
        url, _ = self.addresses(pid)
        try:
            r = self.http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError(f"the provider is not answering ({type(e).__name__})") from e  # its text has the URL
        if r.status_code in (401, 403):
            raise ProviderError("the provider refused the login")
        if r.status_code != 200:
            raise ProviderError(f"the provider answered {r.status_code}")
        ours, links = rewrite(r.text, self.local(pid))
        with self.lock:
            self.cache[pid] = (time.time(), ours, links)
        return ours, links

    def stream(self, pid: str, n: int) -> str:
        _, links = self.playlist(pid)
        if n >= len(links):
            _, links = self.playlist(pid, fresh=True)  # the provider's list changed since Jellyfin read it
        if n >= len(links):
            raise ProviderError("no such channel")
        return links[n]

    def info(self, pid: str) -> list[dict]:
        """A plugin provider's extra lines for its card (Catalog.info), e.g. what it serves other players."""
        found = self.catalogs.get(pid) if self.catalogs else None
        try:
            return found[1].info(self.base) if found else []
        except CatalogError:
            return []

    def kinds(self) -> list[dict]:
        """What "Add a provider" offers besides M3U and Xtream: each installed plugin."""
        found = self.catalogs.kinds if self.catalogs else {}
        return [{"kind": k, "label": p.label, "fields": p.fields} for k, p in found.items()]

    def add(self, body: NewProvider) -> dict:
        if self.catalogs and body.kind in self.catalogs.kinds:
            fields = self.catalogs.kinds[body.kind].fields
            login = {f["key"]: getattr(body, f["key"], "") for f in fields}
            if any(not isinstance(v, str) or len(v) > 500 for v in login.values()):
                raise ProviderError("a field is not text, or is too long")
            if any(not login[f["key"]] for f in fields if not f.get("optional")):
                raise ProviderError("fill in every field")
            try:
                entry = self.catalogs.add(body.name, body.kind, login)
            except CatalogError as e:
                raise ProviderError(f"the provider refused: {e}") from e
            return {"id": entry["id"]}
        if body.kind not in ("m3u", "xtream"):
            raise ProviderError("no such kind of provider here")
        jf = self.jf()
        if jf is None:
            raise ProviderError("Set up Jellyfin first: it plays the channels")
        if body.kind == "m3u":
            if not body.url.startswith(("http://", "https://")):
                raise ProviderError("the playlist address starts with http:// or https://")
            secret, server = {"url": body.url, "epg": body.epg}, ""
        else:
            if not body.server.startswith(("http://", "https://")) or not body.user or not body.password:
                raise ProviderError("an Xtream provider needs its server (http://…), a username and a password")
            secret, server = {"user": body.user, "password": body.password}, body.server.rstrip("/")
        pid = secrets.token_hex(4)
        entry = {"id": pid, "name": body.name, "kind": body.kind} | ({"server": server} if server else {})
        self.vault.put(f"provider:{pid}", secret)
        settings.save({"providers": self.listed() + [entry]}, self.config)
        try:
            ours, _ = self.playlist(pid, fresh=True)  # check it works before Jellyfin is told
            entry["tuner_id"] = jf.add_tuner(f"{self.local(pid)}/playlist.m3u", body.name)
            if upstream(body.kind, server, secret)[1]:
                entry["listings_id"] = jf.add_listings(f"{self.local(pid)}/guide.xml")
            jf.refresh_guide()
        except (ProviderError, JellyfinError):
            self.forget(pid, entry)
            raise
        settings.save({"providers": [e if e["id"] != pid else entry for e in self.listed()]}, self.config)
        return {"id": pid, "channels": ours.count("#EXTINF")}

    def forget(self, pid: str, entry: dict | None = None) -> None:
        """Takes a provider out of Jellyfin, the vault and the settings (whatever of it exists)."""
        entry = entry or self.find(pid)
        jf = self.jf()
        if jf is not None:
            for kind, key in (("tuner", "tuner_id"), ("listings", "listings_id")):
                if entry.get(key):
                    try:
                        jf.remove_tuner(entry[key]) if kind == "tuner" else jf.remove_listings(entry[key])
                    except JellyfinError:
                        pass  # gone already, or Jellyfin is down: the settings entry still goes
        self.vault.delete(f"provider:{pid}")
        self.cache.pop(pid, None)
        settings.save({"providers": [p for p in self.listed() if p["id"] != pid]}, self.config)


def router(providers: Providers, allowed: Callable) -> APIRouter:
    api = APIRouter()
    change = APIRouter(dependencies=[Depends(allowed)])

    def this_mac(request: Request) -> None:
        """The playlist, streams and guide carry the provider's login: only Jellyfin, on this Mac, gets them."""
        if not from_this_mac(request):
            raise HTTPException(403, "only for Jellyfin on this Mac")

    def call(fn, *args):
        try:
            return fn(*args)
        except ProviderError as e:
            raise HTTPException(502 if "answer" in str(e) or "refused" in str(e) else 422, str(e)) from e
        except JellyfinError as e:
            raise HTTPException(502, f"Jellyfin: {e}") from e

    @api.get("/iptv/{pid}/playlist.m3u", dependencies=[Depends(this_mac)])
    def playlist(pid: str):
        return PlainTextResponse(call(providers.playlist, pid)[0], media_type="audio/x-mpegurl")

    @api.get("/iptv/{pid}/stream/{n}", dependencies=[Depends(this_mac)])
    def stream(pid: str, n: int):
        return RedirectResponse(call(providers.stream, pid, n), status_code=302)

    @api.get("/iptv/{pid}/guide.xml", dependencies=[Depends(this_mac)])
    def guide(pid: str):
        _, epg = call(providers.addresses, pid)
        if not epg:
            raise HTTPException(404, "this provider has no guide")
        try:
            r = providers.http.get(epg)
        except httpx.HTTPError as e:
            raise HTTPException(502, "the provider's guide is not answering") from e
        return Response(r.content, status_code=r.status_code, media_type="application/xml")

    @api.get("/api/settings/providers")
    def listed(request: Request):
        """The providers, never their logins. A plugin's info lines (they may hold the login of what it serves) only
        for those who may change settings."""
        labels = {k["kind"]: k["label"] for k in providers.kinds()}
        try:
            allowed(request)
            trusted = True
        except HTTPException:
            trusted = False
        return [
            {k: p[k] for k in ("id", "name", "kind") if k in p}
            | {
                "guide": "listings_id" in p,
                "label": labels.get(p["kind"], ""),
                "info": providers.info(p["id"]) if trusted else [],
            }
            for p in providers.listed()
        ]

    @api.get("/api/settings/providers/kinds")
    def kinds():
        return providers.kinds()

    @change.post("/api/settings/providers", status_code=201)
    def add(body: NewProvider):
        return call(providers.add, body)

    @change.delete("/api/settings/providers/{pid}", status_code=204)
    def remove(pid: str):
        call(providers.forget, pid)

    api.include_router(change)
    return api
