"""Provider plugins: a catalogue of shows, movies and live streams that needs its own code (its pages, its login,
links it signs on demand). Standard IPTV (M3U, Xtream) needs no plugin: providers.py plays it through Jellyfin.

A plugin is a Python package that registers an object under the entry point group `mimir_flix.providers`:

    kind    its id, e.g. "example"; never shown to users
    label   what Settings' "Add a provider" calls it
    fields  what it asks for, kept in the vault: [{"key": "user", "label": "…", "optional": False}, …]; "password"
            and "url" get those input types on the Settings page
    make(login, data) -> Catalog
            login() returns the saved login ({key: value}), read from the vault when it is needed;
            data is a folder of its own for caches and indexes.

The user adds a provider on the Settings page under a name they pick; that name is what the page shows. Core does
everything generic with what a Catalog hands it: the routes (/api/p/<provider id>/…), the resolver Jellyfin's .strm
files point at, the stream relay, .strm files, Kodi, My list, the hourly jobs and the assistant's tools. A plugin may
also answer paths of its own (serve(), e.g. an API for other players) and add lines to its Settings card (info()).

An item, as page(), home() and search() return them:
    {"title", "url", "thumb", "desc", "kind": "folder" | "video", "here": bool, "sub"?: str}
A folder's url is another page of the catalogue; a video's url is what link() turns into something playable.
"""

import base64
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

from mimir_flix import settings

GROUP = "mimir_flix.providers"


class CatalogError(Exception):
    """The provider failed (not reachable, refused the login, an unexpected reply): its text is shown to the user."""


class CatalogDown(CatalogError):
    """The provider says it is down for now (maintenance)."""


@dataclass
class Job:
    """A scheduled job of a plugin, run hourly (the first run at start). Core names it <provider id>-<key> on the
    jobs page. due() says whether the hourly round has anything to do (a check alone is not a run); Run now on the
    jobs page runs it either way. run() returns the run's one-line summary."""

    key: str
    description: str
    schedule: str
    run: Callable[[], str]
    due: Callable[[], bool] = field(default=lambda: True)


class Catalog:
    """What a plugin's make() returns. Methods with a body here are optional."""

    def sections(self) -> list[dict]:
        """Where browsing starts: [{"name", "url"}]."""
        raise NotImplementedError

    def home(self, lang: str) -> list[dict]:
        """Home's rows: [{"section", "url", "items"}], a few items each; a row that fails is left out."""
        raise NotImplementedError

    def page(self, url: str, fresh: bool = False, ttl: int | None = None) -> list[dict]:
        """One folder's items. fresh: read it again now; ttl: the oldest copy (seconds) that may be reused."""
        raise NotImplementedError

    def owns(self, url: str) -> bool:
        """Whether url is a page of this catalogue (anything else is refused before it is fetched)."""
        raise NotImplementedError

    def plays(self, url: str) -> bool:
        """Whether url is a video this catalogue listed. Core refuses any other before link() (and before this Mac
        fetches or relays it), so a caller cannot point it at an address of their choosing, e.g. a router on the
        LAN. The default allows the catalogue's own pages; a plugin whose videos are on other hosts says which."""
        return self.owns(url)

    def link(self, url: str, page: str = "") -> str:
        """A playable link for a video, signed when the provider signs them. page: the list it is in, for a link
        that expires (a fresh one is found there)."""
        raise NotImplementedError

    def check(self) -> None:
        """Signs in once; raises CatalogError when the login does not work (the Settings page's ✓/✗)."""
        raise NotImplementedError

    def search(self, q: str, lang: str = "") -> list[dict]:
        """Results per section: [{"section", "items"}]."""
        return []

    def suggest(self, q: str) -> list[str]:
        """Titles for search suggestions as the user types; local only (it is asked on every key)."""
        return []

    def live(self, q: str) -> list[dict]:
        """Live streams showing q now, best first: [{"title", "section", "url", "page", "quality", "why"}]."""
        return []

    def trail(self, url: str) -> list[str]:
        """The breadcrumb of a folder, from the top; [] when the url does not say."""
        return []

    def relayed(self, url: str) -> bool:
        """Whether this video's stream must go through the relay (plain http, which https pages will not play)."""
        return False

    def on_demand(self, url: str) -> bool:
        """A movie or episode (Jellyfin's movies library) rather than a live stream (its home videos library)."""
        return True

    def kodi(self, url: str, title: str) -> str:
        """What Kodi opens for this video when the provider has its own Kodi add-on; "" plays link() instead."""
        return ""

    def thumb_ok(self, url: str) -> bool:
        """A thumbnail host the proxy may fetch from though the catalogue did not list it since the start."""
        return False

    def section_of(self, url: str) -> str:
        """Which section a show is from, to tell apart two shows of the same name ("TV shows, Spanish")."""
        return ""

    def status(self) -> str:
        """A note for the page while the provider is busy (e.g. rebuilding an index); "" otherwise."""
        return ""

    def jobs(self) -> list[Job]:
        return []

    def everything(self) -> list[dict]:
        """Every movie and show it lists, for the whole catalogue in Jellyfin (mirror.py): [{"kind": "movie" or
        "show", "url", "title", "page"}], optionally with "plot", "thumb" (poster), "language", "genres" (a list,
        e.g. ["Web series"]) and "studio" (a TV channel), which go into Jellyfin's metadata files. A show's url is
        the page its seasons or episodes are on. Read from a local index, not the provider, since it is asked every
        hour. [] leaves the provider out of the mirror."""
        return []

    def info(self, base: str) -> list[dict]:
        """Extra lines for its card on the Settings page, each with a copy button: [{"label", "value"}] (e.g. the
        address and login of what serve() offers other players). base: mimir-flix's address on the home network."""
        return []

    def serve(self, path: str, query: dict[str, str], ctx: "Served"):
        """A GET for a path core does not use, so a plugin can serve other players itself (e.g. an IPTV API). Returns
        None when it is not one of its paths, else a Starlette Response or data sent as JSON. Reachable wherever
        mimir-flix is (the home network and the tailnet), so the plugin checks its own credentials."""
        return None


@dataclass
class Served:
    """What serve() gets from core. play(url, page) is the resolver link for one of its videos (signed at play time,
    relayed when the provider needs it, never logged); base is the address the request came to ("https://host")."""

    play: Callable[[str, str], str]
    base: str


_registered: dict[str, object] = {}


def register(plugin) -> None:
    """A plugin without an entry point (tests)."""
    _registered[plugin.kind] = plugin


def plugins() -> dict[str, object]:
    """Installed plugins by kind."""
    found = {}
    for ep in entry_points(group=GROUP):
        plugin = ep.load()
        found[plugin.kind] = plugin
    return found | _registered


def node_id(pid: str, url: str) -> str:
    """How the page and the assistant name a folder: the provider and the folder's url."""
    return f"{pid}~" + base64.urlsafe_b64encode(url.encode()).decode()


def node_parts(node: str) -> tuple[str, str]:
    """(provider id, url) of a node; ValueError when it is not one."""
    pid, _, enc = node.partition("~")
    if not pid or not enc:
        raise ValueError("not a node")
    try:
        return pid, base64.urlsafe_b64decode(enc.encode()).decode()
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError("not a node") from e


class Catalogs:
    """The plugin providers in Settings, each with its catalogue (made once and kept: caches live in it). A
    provider added or removed on the Settings page is picked up on the next call."""

    def __init__(self, config: Path, vault, data: Path, kinds: dict[str, object] | None = None):
        self.config, self.vault, self.data = config, vault, data
        self.kinds = kinds if kinds is not None else plugins()
        self.made: dict[str, tuple[dict, Catalog]] = {}
        self.lock = threading.Lock()

    def entries(self) -> list[dict]:
        return [p for p in settings.load(self.config)["providers"] if p["kind"] in self.kinds]

    def login(self, pid: str) -> Callable[[], dict]:
        def read() -> dict:
            found = self.vault.get(f"provider:{pid}")
            if not found:
                raise CatalogError("No login saved for this provider (Settings)")
            return found

        return read

    def make(self, entry: dict) -> Catalog:
        folder = self.data / "providers" / entry["id"]
        folder.mkdir(parents=True, exist_ok=True)
        return self.kinds[entry["kind"]].make(self.login(entry["id"]), folder)

    def all(self) -> list[tuple[dict, Catalog]]:
        """(entry, catalog) of every plugin provider, in Settings' order."""
        entries = self.entries()
        with self.lock:
            for e in entries:
                if e["id"] not in self.made or self.made[e["id"]][0] != e:
                    self.made[e["id"]] = (e, self.make(e))
            for gone in set(self.made) - {e["id"] for e in entries}:
                del self.made[gone]
            return [self.made[e["id"]] for e in entries]

    def get(self, pid: str) -> tuple[dict, Catalog] | None:
        return next((x for x in self.all() if x[0]["id"] == pid), None)

    def default(self) -> tuple[dict, Catalog] | None:
        """The first plugin provider: what a link or a My list entry from before providers had ids belongs to."""
        found = self.all()
        return found[0] if found else None

    def resolve(self, pid: str) -> tuple[dict, Catalog] | None:
        return self.get(pid) if pid else self.default()

    def add(self, name: str, kind: str, login: dict) -> dict:
        """Checks the login, then keeps the provider (login in the vault). Raises CatalogError when it fails."""
        if kind not in self.kinds:
            raise CatalogError("no such kind of provider here")
        pid = secrets.token_hex(4)
        entry = {"id": pid, "name": name, "kind": kind}
        self.vault.put(f"provider:{pid}", login)
        try:
            self.make(entry).check()
        except CatalogError:
            self.vault.delete(f"provider:{pid}")
            raise
        settings.save({"providers": settings.load(self.config)["providers"] + [entry]}, self.config)
        return entry
