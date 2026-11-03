"""The LAN listener players fetch from (the TV has no Tailscale), separate from the app's API:

GET /play?p=<provider>&path=<link>  302 to a playable link: on demand freshly signed, live as it is (.strm files);
                                   a relayed stream goes through /hls (plain http); no p: the first provider
GET /hls?u=<stream link>           a live channel's playlists and segments, relayed over this listener
GET /jf/<item id>                  a Jellyfin video's bytes, for Kodi (the API key stays on the Air)
"""

import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from mimir_flix.hls import PASS, HlsError, HlsRelay, from_tailnet, is_hls, is_playlist
from mimir_flix.jellyfin import Jellyfin, JellyfinError
from mimir_flix.catalog import CatalogError, Catalogs

ITEM_ID = re.compile(r"^/jf/([0-9a-f]{32})$")
LINK = re.compile(r"\S+://\S+")
log = logging.getLogger(__name__)


def failed(why: Exception) -> str:
    """Why a play failed, for the log and the player: without any link in it (signed links never go to the log)."""
    reason = LINK.sub("<link>", str(why))[:100]
    log.warning("play failed: %s", reason)
    return reason


def start_resolver(
    catalogs: Catalogs | None, port: int, host: str = "0.0.0.0", jf: Jellyfin | None = None, hls: HlsRelay | None = None
) -> ThreadingHTTPServer:
    hls = hls or HlsRelay()

    class Handler(BaseHTTPRequestHandler):
        def relay(self, url: str):
            """A playlist (rewritten to come back here) or bytes: a segment, or a whole file with the player's Range."""
            ask = {k: v for k, v in self.headers.items() if k.lower() in ("range", "if-range")}
            try:
                r = hls.open(url, ask)
            except HlsError as e:
                self.send_error(404 if "handed out" in str(e) else 502, failed(e))
                return
            try:
                if is_playlist(r):
                    body = hls.rewrite(r.read().decode(errors="replace"), str(r.url), "/hls?u=").encode()
                    self.send_response(r.status_code)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(r.status_code)
                    for k, v in r.headers.items():
                        if k.lower() in PASS:
                            self.send_header(k, v)
                    self.end_headers()
                    if self.command != "HEAD":
                        for chunk in r.iter_bytes(1 << 16):
                            self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, httpx.HTTPError):
                pass  # the player moved on, or the stream dropped
            finally:
                r.close()

        def jellyfin(self, item_id: str):
            headers = {k: v for k, v in self.headers.items() if k.lower() in ("range", "if-range")}
            try:
                r = jf.raw(f"/Videos/{item_id}/stream", {"static": "true"}, headers)
            except JellyfinError:
                self.send_error(502)
                return
            try:
                self.send_response(r.status_code)
                for k, v in r.headers.items():
                    if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges"):
                        self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    for chunk in r.iter_bytes(1 << 16):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the player seeked or stopped
            finally:
                r.close()

        def do_GET(self):
            u = urlsplit(self.path)
            m = ITEM_ID.match(u.path)
            if m and jf:
                self.jellyfin(m.group(1))
                return
            query = parse_qs(u.query)
            if u.path == "/hls":
                self.relay(query.get("u", [""])[0])
                return
            raw, page = query.get("path", [""])[0], query.get("page", [""])[0]
            found = catalogs.resolve(query.get("p", [""])[0]) if catalogs else None
            if u.path != "/play" or not raw.startswith("https://") or found is None:
                self.send_error(404)
                return
            cat = found[1]
            if not cat.plays(raw):  # a video the provider listed, never an address a caller made up
                self.send_error(404)
                return
            try:
                link = cat.link(raw, page if cat.owns(page) else "")
            except CatalogError as e:
                self.send_error(502, failed(e))
                return
            away = from_tailnet(self.client_address[0], self.headers.get("X-Forwarded-For"))
            if cat.relayed(raw) or (away and is_hls(link)):  # plain http, or away from home: relayed through here
                link = "/hls?u=" + quote(hls.allow(link), safe="") + "&x=.m3u8"
            elif away:  # a file, away from home: the Air streams it (the player's Range passed on, so seeking works)
                self.relay(hls.allow(link))
                return
            self.send_response(302)
            self.send_header("Location", link)
            self.end_headers()

        do_HEAD = do_GET

        def log_message(self, fmt, *args):  # signed links never go to the log
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
