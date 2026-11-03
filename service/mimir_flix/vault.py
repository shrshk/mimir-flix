"""The vault: every login mimir-flix takes in (providers, Kodi) is kept encrypted in its own database,
never in settings.json or the log.

AES-GCM with a 256-bit key; each secret has its own nonce, and its name is bound in as associated data, so a value
cannot be moved to another name. The key lives outside the database: in the macOS Keychain (service
"mimir-flix-vault"), or, where there is no Keychain, in an owner-only file beside settings.json. A copy of the
database alone (a backup) holds nothing readable.
"""

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEYCHAIN_SERVICE = "mimir-flix-vault"
SCHEMA = """CREATE TABLE IF NOT EXISTS vault (
    name TEXT PRIMARY KEY,  -- e.g. provider:<id>, kodi
    nonce BLOB NOT NULL,
    data BLOB NOT NULL,     -- AES-GCM of the JSON value
    updated REAL NOT NULL
)"""


class VaultError(Exception):
    pass


def keychain_key() -> bytes | None:
    """The key from the macOS Keychain, made there on first use; None where there is no Keychain."""
    if not shutil.which("security"):
        return None
    try:  # a Keychain that asks (a dialog nobody answers at night) must not hang the app: the file key then
        found = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "key", "-w"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if found.returncode == 0 and found.stdout.strip():
            return base64.b64decode(found.stdout.strip())
        key = AESGCM.generate_key(bit_length=256)
        made = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                "key",
                "-w",
                base64.b64encode(key).decode(),
            ],
            capture_output=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return None
    return key if made.returncode == 0 else None


def file_key(path: Path, make: bool = True) -> bytes:
    """The key from an owner-only file, made on first use (no Keychain: Linux, or a Keychain that refused)."""
    if not path.exists() and not make:
        raise VaultError(
            "the vault's key is in the Keychain, which cannot be read here (over SSH it is locked): run this in the "
            "Mac's login session, e.g. as a launchd job"
        )
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o077)
        try:
            path.write_text(base64.b64encode(AESGCM.generate_key(bit_length=256)).decode())
        finally:
            os.umask(old)
    return base64.b64decode(path.read_text().strip())


class Vault:
    def __init__(self, db: Path, key: Callable[[], bytes]):
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute(SCHEMA)
        self._key_source, self._aes = key, None

    @classmethod
    def for_mac(cls, db: Path, config: Path) -> "Vault":
        # A key file is made only for an empty vault: one with secrets has its key in the Keychain (or the file),
        # and a Keychain this session cannot read (over SSH it is locked) must not lead to a second key.
        vault = cls(db, lambda: keychain_key() or file_key(config / "vault-key", make=vault.empty()))
        return vault

    def empty(self) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM vault LIMIT 1").fetchone() is None

    def _cipher(self) -> AESGCM:
        if self._aes is None:
            self._aes = AESGCM(self._key_source())
        return self._aes

    def put(self, name: str, value: dict) -> None:
        nonce = os.urandom(12)
        data = self._cipher().encrypt(nonce, json.dumps(value).encode(), name.encode())
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO vault VALUES (?, ?, ?, ?)", (name, nonce, data, time.time()))

    def get(self, name: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT nonce, data FROM vault WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(self._cipher().decrypt(row[0], row[1], name.encode()))
        except InvalidTag as e:
            raise VaultError(f"the vault cannot open {name} (another key, or changed)") from e

    def delete(self, name: str) -> bool:
        with self.lock, self.db:
            return self.db.execute("DELETE FROM vault WHERE name = ?", (name,)).rowcount > 0

    def has(self, name: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM vault WHERE name = ?", (name,)).fetchone() is not None
