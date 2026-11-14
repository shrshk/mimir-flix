# mimir-flix

Your own streaming front end on top of Jellyfin: one place to explore and search your own videos, see which of
your streaming apps has a title, and choose what Jellyfin shows. mimir-flix is the explorer; Jellyfin plays your
videos (on the phone in Swiftfin or the browser, on the TV in the Jellyfin app) and keeps track of what was watched,
and a streaming title opens in its app.

Providers are optional: live TV from IPTV (an M3U playlist or an Xtream Codes login, played by Jellyfin's Live TV),
and providers with a catalogue of their own through a provider plugin (see below). Kodi and the TV add ways to play. Your streaming apps (Netflix, Prime Video, Peacock …) are a filter: search counts only the ones
you have.

## Quick start (a new Mac)

```sh
git clone https://github.com/shrshk/mimir-flix && cd mimir-flix
scripts/setup-mac.sh
```

It installs uv and Jellyfin (Homebrew), starts mimir-flix at login (`scripts/install-mac.sh`) and opens the
Setup page (`http://127.0.0.1:8723/setup`): what the Mac needs, then one step at a time, each skippable except
Jellyfin:

1. Jellyfin: finishes its first-run wizard if needed (the admin user), turns on the Mac's hardware video
   (VideoToolbox) and makes mimir-flix's Jellyfin API key (`~/.config/mimir-flix/jellyfin-api-key`, 600);
2. your media (libraries on the Library page), your streaming apps (a TMDB token in
   `~/.config/mimir-flix/tmdb-token`, 600), the TV (and Kodi), how phones and the TV reach mimir-flix, and the
   nightly backup (optionally copied off-site, encrypted).

No domain of your own is needed. Phones (and other computers) open mimir-flix at
`https://<this Mac's Tailscale name>:8726`: Tailscale's own certificate, your tailnet only, never public (`tailscale
serve`, turned on from Setup or Settings, which show the link and a QR code to scan). The TV has no Tailscale, so it
opens mimir-flix at this Mac's home address (`http://192.168.1.x:8723`), which the TV app asks for once. A domain of
your own is the optional, advanced way (below). Without one, a provider's videos added to Jellyfin play in Jellyfin's
apps at home (they open the resolver at the home address); away from home, play them in mimir-flix, or let the
tailnet reach the home network (Tailscale subnet route).

Settings go to `~/.config/mimir-flix/settings.json`; passwords never do. Setup locks once done; after that the
Settings page (`/settings`, ⚙ in the header) shows what works with the fix for what does not, and changes anything,
including more TVs (pair each once: accept the prompt on the TV), providers (IPTV, or a plugin's kind, under a name
you pick) and the nightly backup folder. Changes come only from the Mac itself or a tailnet device. Every login
taken in (providers, Kodi) is kept encrypted in mimir-flix's database,
the key in the Keychain of the Mac running mimir-flix (made there on first use); backups hold no keys or
passwords.

## Commands

| Command | What it does |
|---|---|
| `scripts/setup-mac.sh` | Everything above, on a new Mac (asks before installing anything) |
| `/setup` in the browser | What this Mac needs (Homebrew, uv, Jellyfin, Tailscale), with an Install button that works only on the Mac itself and asks first; then setup one step at a time |
| `/settings` in the browser | What works (✓/✗ with the fix) and every setting: Jellyfin, providers, your apps, TVs (pair each), Kodi, phones |
| `scripts/restore-mac.sh <backup>` | Puts a nightly backup back (asks first; keeps what is there as *.before-restore) |
| `/tv/app.apk` | The TV app ([mimir-flix-tv](https://github.com/shrshk/mimir-flix-tv), its own repo) for the TV's Downloader app; Settings → TV app has the steps |
| `scripts/install-mac.sh` | Install or update the app (`--remove` to remove it; files and settings stay) |
| `mimir-flix-tv-setup <tv>` | Another TV set up like the first (below) |

### Another TV

Most of what makes the TV work lives in Jellyfin on this Mac and follows the Jellyfin user to any TV: Wholphin's home
rows, the catalogue's collections by language, the apps' "Popular on" playlists, titles, posters and badges. A new TV
needs its apps, a sign-in and a screensaver:

1. Pair the TV on the Settings page (TVs), once.
2. `mimir-flix-tv-setup <tv>`: opens the Play Store page of each missing app on the TV ([Wholphin](https://github.com/damontecres/Wholphin),
   [Immich TV](https://github.com/giejay/Immich-Android-TV)), one at a time (press Install, run it again), and makes Immich TV the screensaver.
3. In Wholphin, pick the server and Add user: it shows a Quick Connect code. `mimir-flix-tv-setup <tv> --approve CODE`
   signs it in as the house's Jellyfin user, with no password typed on the TV.
4. Wholphin: Settings → Home → use the server's layout. Immich TV: sign in, then Settings → Screensaver → the albums.

Wholphin's own app settings (playback, theme) stay on each TV.

## Settings

`~/.config/mimir-flix/settings.json` (`MIMIR_FLIX_CONFIG` moves the folder). Every key is optional;
`MIMIR_FLIX_<KEY>` in the environment overrides the file. Example: [settings.example.json](settings.example.json).

| Key | Default | Meaning |
|---|---|---|
| `jellyfin_url` | `http://127.0.0.1:8096` | Jellyfin |
| `host`, `port` | `127.0.0.1`, `8723` | Where mimir-flix listens. `0.0.0.0` to reach it from phones directly (no https) |
| `media_root` | `~/Documents/media` | A plugin provider's `.strm` files: `<id>/`, `<id>-movies/`, `<id>-live/` |
| `resolver` | `http://127.0.0.1:8724` | The address players reach a provider's links at (see below) |
| `resolver_port` | `8724` | The port the resolver listens on |
| `tvs` | (none) | The TVs, `[{"name", "host", "kind": "android" \| "fire"}]`; the first is the default for 📺 TV (`tv_host`, one TV's IP, still works) |
| `kodi_url`, `kodi_password_file` | (none) | Kodi on the TV (user `kodi`), the other way to play on the TV |
| `streaming_apps` | (all) | The streaming apps you have, e.g. `netflix,prime,peacock` (keys in `streaming.py`'s `APPS`) |
| `streaming_region` | `US` | The country whose streaming catalogues count |
| `providers` | (none) | Providers (IPTV, or a plugin's kind), added on the Settings page; their logins live in the vault, never here |
| `backup_dir` | (off) | The folder for the nightly backup (3:30): mimir-flix's and Jellyfin's settings, lists and watch history |
| `jellyfin_data` | `~/Library/Application Support/jellyfin` | Where this Mac's Jellyfin keeps its data (for the backup) |
| `mirror_languages`, `mirror_per_category` | (all), `0` | The whole catalogue in Jellyfin keeps only these languages and the newest N titles per category; titles outside go to the Trash unless added by hand, watched or favourited |
| `recs_avoid_genres`, `recs_languages` | (none) | Recommendations leave out these genres and keep only these original languages (`te`, `hi`, `en` …) |

Secrets never go in settings.json: providers' logins are in the vault; the Jellyfin API key, TMDB token and ADB key
are files readable by you only.

## What you get

- **Watch**: a featured title, rows of your libraries and of each provider's sections (a fold per provider), and
  search over all of it (each provider's own search, results labelled with its name).
- **Playing**: ▶ Jellyfin plays it in Jellyfin: on an iPhone it opens Swiftfin at the item and the Air starts it
  playing there (Jellyfin's remote control; Swiftfin 1.6 or later), elsewhere Jellyfin's web app. 📺 TV starts it in
  the TV's Jellyfin app (waking the TV and opening the app if needed). Play here plays it in the page's own player
  (picture in picture, AirPlay, the lock screen): through Jellyfin, it starts where any Jellyfin app left off, saves
  its place and played marks there (so Continue watching is the same on the TV), and goes on to the next episode after
  a short countdown. Kodi is the other option. For a provider's title, ＋ Jellyfin first adds it as `.strm` files (a
  show, a movie or a live channel); items Jellyfin has show ✓ (asked from Jellyfin's API, so something removed there
  shows as not added). New episodes of added shows reach Jellyfin within the hour.
- **Your streaming apps**: search also finds titles on the apps you have (from TMDB, which takes JustWatch's
  data), and a title in Jellyfin or a provider shows once, with its apps. The app's button opens it: on the phone its
  link (the app opens), on the TV (or 📺 from the phone) the app itself, at its search for the title where it can.
- **For you**: recommendations by category (Buzzing now, Worth the hype, Skip the hype, Crowd favourites, Hidden
  gems, New on your apps) from TMDB, narrowed to your apps (or any, or some), a language, a genre, movies or shows;
  what was watched in Jellyfin is left out. A row on Home; the assistant's `recommend` tool uses the same.
- **My list**: ＋ My list on any title (your library, a streaming app or a provider). Titles in your library are
  Jellyfin favourites, so every Jellyfin app shows the same ones; a provider's shows on it count their new episodes. 👍 / 👎 on a streaming title steer recommendations (👎 leaves it out everywhere).
- **Library**: libraries, folders (everything under a folder is included), excluded subfolders (an empty
  `.ignore` file, which Jellyfin skips), single videos (linked into the library's picks folder), and live scan status.
- **Jobs and backups**: `/jobs` lists the scheduled jobs (nightly backup, new episodes, each provider's own) with
  each run, Pause and Run now. A job that fails twice in a row sends one notice through
  [jobkit](https://github.com/shrshk/jobkit): set `~/.config/jobkit/notify-url` to `openclaw:<phone>` (a push to
  the OpenClaw iPhone app, title only) or an ntfy-style URL. `scripts/restore-mac.sh` puts a backup back.
- **An assistant can use it (MCP)**: `POST /mcp` lets an MCP client such as OpenClaw search, check what the TV
  shows, play on the TV, and use My list. Tools: search, whats_on_tv, play_on_tv, live_now, whats_new, trending,
  add_to_jellyfin, my_list, add_to_my_list, remove_from_my_list; for recommendations, recommend, taste, history,
  candidates, find_titles, similar_to, title_details and feedback (👍/👎). mimir-flix has no model: the assistant
  does the judging and shows its picks in its own chat, and writes to mimir-flix only when you say so (My list,
  👍/👎). A ready-made OpenClaw skill for daily picks is in mimir-claw. A provider's titles come with `provider`
  and `id`, which play_on_tv, add_to_jellyfin and My list take back. live_now asks each provider that has live
  streams for the one showing a match now ("Arsenal"). It needs the bearer token in `~/.config/mimir-flix/mcp-token`
  (made on first start, owner-only). Through Caddy, only Tailscale addresses get in. Example client entry:
  `{"url": "http://127.0.0.1:8723/mcp", "headers": {"Authorization": "Bearer <the token>"}}`.

### Provider plugins

A provider with its own catalogue (pages to browse, its own search, links it signs on demand) needs a plugin: a
Python package that registers a `kind` under the `mimir_flix.providers` entry point group and returns a `Catalog`
(`service/mimir_flix/catalog.py` describes both; `service/tests/test_catalog.py` has a small fake one). Core plays,
relays or adds to Jellyfin only videos the plugin says it listed (`Catalog.plays`, by default its own pages'
addresses), so no caller can make the Mac fetch an address of their choosing. List
installed plugins in `~/.config/mimir-flix/plugins`, one per line (a folder or a package spec), and run
`scripts/install-mac.sh`. Settings → Providers then offers the plugin's kind: give it a name and its login, which is
checked before it is kept (a plugin may ask for more, such as its Kodi add-on's id; those fields can be optional).
Without a plugin installed, its provider is left out and everything else works.

**Kodi add-ons.** mimir-flix can play a title through a Kodi add-on on the TV: a plugin's `kodi()` returns the
add-on's `plugin://…` link, and Kodi plays it with the add-on doing its own sign-in. A plugin that leaves `kodi()`
empty gets its playable link sent to Kodi instead.

**Serving other players.** A plugin may also answer web addresses of its own (`Catalog.serve`), for example an
IPTV playlist or an Xtream Codes-style API for Kodi and other IPTV apps, and show what players need on its Settings
card (`Catalog.info`, each line with a Copy button). Its play links go through the resolver like everything else, so
links are signed at play time and nothing is stored. Only reachable where mimir-flix is (the home network and the
tailnet); the plugin checks its own login.

**Live TV in Kodi.** When a provider's Settings card shows an M3U playlist link, Kodi can list its channels under
**TV**, grouped by category:

1. In Kodi: Add-ons → Install from repository → Kodi Add-on repository → PVR clients → PVR IPTV Simple Client →
   Install (OK to the extra add-ons it needs: InputStream Adaptive, FFmpeg Direct, RTMP Input).
2. Its settings (Add-ons → My add-ons → PVR clients → PVR IPTV Simple Client → Configure): General → Location
   "Remote path (Internet address)", M3U playlist URL: paste the link from Settings' Copy button. Optionally, set
   "Auto refresh mode" to once a day so new channels show up.
3. Restart Kodi (or turn the add-on off and on). TV → Channels lists them; press Left for the side menu and pick a
   group under "Group".

There is no guide unless the provider has one. Kodi on a Fire TV is set up the same way.

Each provider gets its own section on Home, search results, My list entries, jobs (`<id>-<job>` on the jobs page)
and three Jellyfin libraries named after it (`<name>`, `<name> Movies`, `<name> Live`).

### How a provider's links play

Jellyfin's `.strm` files point at the resolver (`resolver`), which asks the provider for a playable link when a
player asks (signing it, where the provider signs links). A stream the provider hands over as plain http on an IP
address, which https players refuse, is relayed over the resolver's own address. A player away from home (a
Tailscale address, 100.64.0.0/10) gets everything streamed by this Mac rather than the provider's servers. Only
stream servers a provider handed out are relayed.

### Your own domain (optional, advanced)

Safari, Swiftfin's native player and Jellyfin's web app on https need https links, and a Home Screen install
needs https too. One way: Caddy on this Mac with a wildcard certificate (DNS challenge), names pointing at the
Mac's Tailscale address, and `lan.<domain>` at its LAN address so the TV (no Tailscale) reaches the resolver
(step by step, with the Cloudflare token and DNS records: [Your own domain with Cloudflare](https://gist.github.com/shrshk/58b710264907d1e229675a2313f24b0e)):

```
*.home.example.com {
	tls { dns cloudflare {env.CLOUDFLARE_API_TOKEN} }
	@flix host flix.home.example.com
	handle @flix { reverse_proxy 127.0.0.1:8723 }
	@jellyfin host jellyfin.home.example.com
	handle @jellyfin { reverse_proxy 127.0.0.1:8096 }
	@lan host lan.home.example.com
	handle @lan { reverse_proxy 127.0.0.1:8724 }
}
```

Then `"resolver": "https://lan.home.example.com"` and `"hosts": ["flix.home.example.com"]`: the app answers only to
addresses, `.local` and tailnet names, the resolver's name and the names in `hosts`. Caddy sets `X-Forwarded-For`, which the resolver uses
to tell home from away. Without it, set `host` to `0.0.0.0` and use `http://<mac>.local:8723` on the home network.

Runs natively on purpose: on a Mac, Jellyfin in Docker loses hardware video, and this app only needs Python.

## Security

mimir-flix is for one household's devices. It is not built to face the internet.

- The app listens on `127.0.0.1` by default. Phones reach it through `tailscale serve` (your tailnet only); with
  "on the home network" ticked it also listens there over plain http, for the TV. Or reach it through Caddy, with only Tailscale
  addresses let in (above). Don't port-forward it or put it behind a public tunnel.
- Every device on your tailnet counts as yours, including any shared in from someone else's tailnet: limit who
  reaches this Mac with Tailscale's access rules (ACLs) if you share devices.
- Settings, libraries and jobs change only from the Mac itself or a tailnet device. Any device on the Wi-Fi can
  browse and play.
- The app answers only to its own names (`hosts` in settings.json, see above), so a web page cannot point its own
  name at the Mac and reach it (DNS rebinding). Browsers' requests from another site cannot change anything.
- The resolver (port 8724) listens on the LAN for the TV, which has no Tailscale. It serves play redirects, live
  relays and Jellyfin video bytes, never settings or keys. Keep it off the internet too.
- The MCP endpoint needs the bearer token in `~/.config/mimir-flix/mcp-token`, and a tailnet or local address.
- Secrets are never in `settings.json`. Provider logins are in an encrypted vault; the Jellyfin API key, TMDB
  token, ADB key and MCP token are owner-only (600) files under `~/.config/mimir-flix`.
- Nightly backups hold mimir-flix's database and Jellyfin's database and config, not the key files. Jellyfin's
  database does hold its API keys and devices' sign-ins, so each archive is owner-only. Keep the backup folder on a
  disk only you can read, and send it off-site only encrypted (an rclone crypt remote).

Report a security problem privately through GitHub's "Report a vulnerability" on this repo, not in an issue.

## License

MIT, see [LICENSE](LICENSE).

## Architecture

What runs where, the main paths, and a code map: [docs/architecture.md](docs/architecture.md).

## Develop

```sh
cd service && uv sync && uv run pytest
```
