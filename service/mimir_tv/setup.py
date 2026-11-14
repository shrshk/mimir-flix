"""Setting up Jellyfin and keeping secrets, for the Setup and Settings pages (wizard.py): Jellyfin's own first run
(creating the admin user), mimir-tv's API key with the Mac's hardware video decoding (VideoToolbox) turned on,
and owner-only secret files. Logins are kept in the vault (vault.py). Passwords are used, never written down here.
"""

import httpx

CLIENT = 'MediaBrowser Client="mimir-tv-setup", Device="setup", DeviceId="mimir-tv-setup", Version="1"'


def jellyfin_first_run(url: str, name: str, password: str, transport: httpx.BaseTransport | None = None) -> None:
    """Jellyfin's startup wizard, through its API: language, the admin user, remote access on, done."""
    with httpx.Client(base_url=url, timeout=30, transport=transport) as c:
        c.post(
            "/Startup/Configuration",
            json={"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"},
        ).raise_for_status()
        c.get("/Startup/User").raise_for_status()
        c.post("/Startup/User", json={"Name": name, "Password": password}).raise_for_status()
        c.post("/Startup/RemoteAccess", json={"EnableRemoteAccess": True}).raise_for_status()
        c.post("/Startup/Complete").raise_for_status()


class SignInRefused(Exception):
    pass


def jellyfin_key(url: str, name: str, password: str, transport: httpx.BaseTransport | None = None) -> str:
    """Signs in as the admin once, creates the key "mimir-tv" and returns it; turns on hardware video."""
    with httpx.Client(base_url=url, timeout=30, headers={"Authorization": CLIENT}, transport=transport) as c:
        r = c.post("/Users/AuthenticateByName", json={"Username": name, "Pw": password})
        if r.status_code != 200:
            raise SignInRefused("Jellyfin refused that user name and password")
        c.headers["Authorization"] = f'{CLIENT}, Token="{r.json()["AccessToken"]}"'
        c.post("/Auth/Keys", params={"app": "mimir-tv"}).raise_for_status()
        keys = [k for k in c.get("/Auth/Keys").json()["Items"] if k.get("AppName") == "mimir-tv"]
        encoding = c.get("/System/Configuration/encoding").json()
        # Hardware decoding only: VideoToolbox refuses to encode when Jellyfin asks for a live channel's own
        # (low) level at an unknown, so maximum, bitrate; software encoding takes any request.
        want = {"HardwareAccelerationType": "videotoolbox", "EnableHardwareEncoding": False}
        if any(encoding.get(k) != v for k, v in want.items()):
            c.post("/System/Configuration/encoding", json=encoding | want).raise_for_status()
        return keys[-1]["AccessToken"]


def save_secret(path, value: str) -> None:
    """A secret in its own file, readable by this user only from the moment it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(value + "\n")
