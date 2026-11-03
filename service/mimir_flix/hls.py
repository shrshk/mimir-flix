"""Relays streams through this Mac: a live channel's HLS over https, and (for a player away from home) any stream.

Some providers hand live channels over as plain http on an IP address and port; Safari and Jellyfin's web app (both
on https pages) refuse to play that. Playlists are rewritten so every variant, segment and key comes back through
here. Only stream servers a provider itself handed out (allow) are relayed, so this is not an open proxy.

Away from home (a Tailscale address) the phone gets everything from this Mac instead of the provider's servers: this
Mac asked for the link, so a link tied to the network that asked still plays, and the home upload has room to spare.
"""

import ipaddress
import re
import socket
import threading
from urllib.parse import quote, urljoin, urlsplit

import httpx

URI_ATTR = re.compile(r'URI="([^"]+)"')
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))  # Tailscale v4, v6
PASS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")


def from_tailnet(peer: str, forwarded: str | None = None) -> bool:
    """A player away from home: a Tailscale address. Behind Caddy (the peer is this machine) the player's own
    address is the first one in X-Forwarded-For (Caddy replaces any the player sent: keep it so, no trusted_proxies)."""
    ip = peer
    if peer in ("127.0.0.1", "::1") and forwarded:
        ip = forwarded.split(",")[0].strip()
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in TAILNET if net.version == addr.version)
    except ValueError:
        return False


def inside(host: str) -> bool:
    """This Mac itself (loopback, any address) or a link-local one: never a stream server, whatever a playlist or a
    redirect says (this app and Jellyfin listen there, and trust this Mac). Home addresses stay allowed: a tuner
    on the LAN is a stream server."""
    try:
        found = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return False  # not resolvable: the request fails on its own
    for a in found:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return True
    return False


def is_hls(url: str) -> bool:
    return urlsplit(url).path.endswith(".m3u8")


class HlsError(Exception):
    pass


class HlsRelay:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.hosts: set[str] = set()
        self.lock = threading.Lock()
        self.http = httpx.Client(
            timeout=20, follow_redirects=True, transport=transport, event_hooks={"request": [self.refuse_inside]}
        )

    @staticmethod
    def refuse_inside(request: httpx.Request) -> None:
        """Every request, redirects included: never to this Mac (a hostile stream server's way to its other apps)."""
        if inside(request.url.host):
            raise HlsError("a stream may not point at this Mac")

    def allow(self, url: str) -> str:
        with self.lock:
            self.hosts.add(urlsplit(url).netloc)
        return url

    def allowed(self, url: str) -> bool:
        u = urlsplit(url)
        with self.lock:
            return u.scheme in ("http", "https") and u.netloc in self.hosts

    def open(self, url: str, headers: dict | None = None) -> httpx.Response:
        """The upstream response, streamed; the caller closes it. headers: the player's Range (seeking in a file).
        Raises HlsError for a server not handed out."""
        if not self.allowed(url):
            raise HlsError("not a stream this app handed out")
        try:
            r = self.http.send(self.http.build_request("GET", url, headers=headers), stream=True)
        except httpx.HTTPError as e:
            raise HlsError(f"the channel's stream is not answering: {e}") from e
        # The name was checked before connecting; a server whose name then pointed here (a short DNS lifetime) is
        # caught by the address actually connected to, before any of its answer is passed on.
        stream = r.extensions.get("network_stream")
        peer = stream.get_extra_info("server_addr") if stream is not None else None
        if peer and inside(peer[0]):
            r.close()
            raise HlsError("a stream may not point at this Mac")
        return r

    def rewrite(self, playlist: str, base: str, prefix: str) -> str:
        """Every URI in the playlist (lines and URI="…" attributes), made absolute and pointed back at prefix."""

        def relay(uri: str) -> str:
            absolute = urljoin(base, uri)
            self.allow(absolute)  # a variant on another server of the same stream
            # ffmpeg (Jellyfin) only reads segments whose address ends in a media extension: end with the original's.
            ext = urlsplit(absolute).path.rsplit(".", 1)[-1].lower()
            ext = (
                "mp4" if ext == "fmp4" else ext if ext.isalnum() and len(ext) <= 5 else "ts"
            )  # .fmp4 is not on its list
            return f"{prefix}{quote(absolute, safe='')}&x=.{ext}"

        out = []
        for line in playlist.splitlines():
            if line.startswith("#"):
                out.append(URI_ATTR.sub(lambda m: f'URI="{relay(m.group(1))}"', line))
            elif line.strip():
                out.append(relay(line.strip()))
            else:
                out.append(line)
        return "\n".join(out) + "\n"


def is_playlist(response: httpx.Response) -> bool:
    kind = response.headers.get("content-type", "").lower()
    return "mpegurl" in kind or urlsplit(str(response.url)).path.endswith(".m3u8")
