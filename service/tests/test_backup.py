import sqlite3
import threading
import time
import tarfile
from datetime import date

import pytest

from mimir_tv import backup


def make_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t (v TEXT)")
    db.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    db.commit()
    return db  # left open, as the running apps keep theirs


def test_a_backup_keeps_lists_settings_and_watch_history_never_artwork_or_secrets(tmp_path):
    data, config, jf, dest = tmp_path / "data", tmp_path / "config", tmp_path / "jellyfin", tmp_path / "backups"
    dest.mkdir()
    open_db = make_db(data / "mimir-tv.db", ["My list: Tandel"])
    make_db(jf / "data" / "jellyfin.db", ["played: Dark S1E3"])
    config.mkdir()
    (config / "settings.json").write_text('{"tv_host": "192.0.2.25"}')
    for secret in ("jellyfin-api-key", "adbkey", "mcp-token", "tmdb-token", "vault-key"):
        (config / secret).write_text("SECRET")
    (jf / "config").mkdir()
    (jf / "config" / "system.xml").write_text("<ServerConfiguration/>")
    (jf / "metadata" / "library").mkdir(parents=True)
    (jf / "metadata" / "library" / "poster.jpg").write_bytes(b"\xff" * 100)

    out = backup.back_up(data, config, jf, dest, today=date(2026, 11, 2))
    assert out.name == "mimir-tv-2026-11-02.tar.gz"
    with tarfile.open(out) as tar:
        names = set(tar.getnames())
        tar.extractall(tmp_path / "x", filter="data")
    assert {
        "mimir-tv/mimir-tv.db",
        "mimir-tv/settings.json",
        "jellyfin/data/jellyfin.db",
        "jellyfin/config/system.xml",
    } <= names
    assert not any("metadata" in n or "SECRET" in n for n in names)
    assert not any(n.endswith(s) for n in names for s in ("adbkey", "mcp-token", "vault-key", "api-key"))
    restored = sqlite3.connect(tmp_path / "x" / "mimir-tv" / "mimir-tv.db")
    assert restored.execute("SELECT v FROM t").fetchall() == [("My list: Tandel",)]  # consistent while open
    played = sqlite3.connect(tmp_path / "x" / "jellyfin" / "data" / "jellyfin.db")
    assert played.execute("SELECT v FROM t").fetchall() == [("played: Dark S1E3",)]
    open_db.close()


def test_the_newest_seven_are_kept_and_a_missing_drive_is_said_plainly(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "backups"
    dest.mkdir()
    make_db(data / "mimir-tv.db", ["x"]).close()
    for day in range(1, 10):
        backup.back_up(data, tmp_path / "config", None, dest, today=date(2026, 11, day))
    assert [b["name"] for b in backup.listed(dest)][0] == "mimir-tv-2026-11-09.tar.gz"
    assert len(backup.listed(dest)) == 7 and not (dest / "mimir-tv-2026-11-02.tar.gz").exists()
    assert all(f.stat().st_mode & 0o077 == 0 for f in dest.iterdir())  # owner-only: Jellyfin's sign-ins are inside
    with pytest.raises(backup.BackupError, match="drive connected"):
        backup.back_up(data, tmp_path / "config", None, tmp_path / "unplugged")


def test_a_drive_that_does_not_answer_is_said_at_once_not_waited_on(tmp_path, monkeypatch):
    # macOS holds the first look at an external drive until someone allows it: that wait never ends on its own.
    held, looks = threading.Event(), []

    def stuck(dest):
        looks.append(dest)
        held.wait()
        return []

    monkeypatch.setattr(backup, "listed", stuck)
    started = time.monotonic()
    assert backup.listed_soon(tmp_path, wait=0.2) == ([], backup.STUCK)
    assert backup.listed_soon(tmp_path, wait=0.2) == ([], backup.STUCK)
    assert time.monotonic() - started < 2 and len(looks) == 1  # one stuck look, not one per page load
    with pytest.raises(backup.BackupError, match="not answering"):
        backup.back_up(tmp_path, tmp_path, None, tmp_path)  # the nightly job fails instead of hanging
    held.set()


FAKE_RCLONE = """#!/bin/sh
# rclone with the remote as a local folder: sync copies, check and size answer like rclone does
echo "$@" >> "$CALLS"
case "$1" in
  sync) rm -rf "$REMOTE" && cp -R "$2" "$REMOTE" ;;
  check) diff -rq "$2" "$REMOTE" >/dev/null ;;
  size) echo '{"count": 1, "bytes": 2048}' ;;
esac
"""


def nightly_jobs(tmp_path, monkeypatch, backup_dir):
    from jobkit import Jobs

    from mimir_tv.app import start_backups

    rclone = tmp_path / "rclone"
    rclone.write_text(FAKE_RCLONE)
    rclone.chmod(0o755)
    monkeypatch.setenv("CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("REMOTE", str(tmp_path / "remote"))
    monkeypatch.setattr("jobkit.backup.LOCK_DIR", tmp_path / "locks")
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    make_db(data / "mimir-tv.db", ["x"]).close()
    (config / "settings.json").write_text(
        f'{{"backup_dir": "{backup_dir}", "offsite": "backup:mimir-tv", "rclone": "{rclone}"}}'
    )
    jobs = Jobs(tmp_path / "jobs.db", app="mimir-tv")
    start_backups(jobs, data, config)
    jobs.actions[backup.JOB]()
    return {j["name"]: j["last"] for j in jobs.report()}


def test_each_clean_backup_goes_off_site_after_it_and_is_checked(tmp_path, monkeypatch):
    dest = tmp_path / "backups"
    dest.mkdir()
    last = nightly_jobs(tmp_path, monkeypatch, dest)
    assert last[backup.JOB]["status"] == "ok"
    assert last[backup.OFFSITE]["status"] == "ok" and last[backup.OFFSITE]["summary"].endswith("copy matches")
    assert [p.name for p in (tmp_path / "remote").iterdir()] == [p.name for p in dest.iterdir()]  # today's archive
    assert [c.split()[0] for c in (tmp_path / "calls").read_text().splitlines()] == ["size", "sync", "check"]  # counted first: nothing to send is never synced


def test_a_failed_backup_is_never_synced_off_site(tmp_path, monkeypatch):
    last = nightly_jobs(tmp_path, monkeypatch, tmp_path / "unplugged")
    assert last[backup.JOB]["status"] == "failed"
    assert last[backup.OFFSITE] is None and not (tmp_path / "calls").exists()  # no run, no rclone
