#!/usr/bin/env bash
# Puts a nightly backup back (D9 step 6): mimir-tv's database and settings, and Jellyfin's database and settings.
#   scripts/restore-mac.sh /Volumes/Drive/Backups/mimir-tv/mimir-tv-2026-11-02.tar.gz
# Asks first. Stops both apps, keeps what is there now as *.before-restore, puts the backup in place, starts them.
# On a new Mac afterwards: Setup's Jellyfin step for a new API key, and save the logins again on the Settings page
# (the vault's key stayed in the old Mac's Keychain; backups never hold keys or passwords).
set -euo pipefail
ARCHIVE=${1:?usage: restore-mac.sh <mimir-tv-YYYY-MM-DD.tar.gz>}
DATA=${MIMIR_TV_DATA:-$HOME/Library/Application Support/mimir-tv}
CONFIG=${MIMIR_TV_CONFIG:-$HOME/.config/mimir-tv}
JF="$HOME/Library/Application Support/jellyfin"
[ -f "$ARCHIVE" ] || { echo "no such file: $ARCHIVE" >&2; exit 1; }
read -r -p "Replace mimir-tv's and Jellyfin's data on this Mac with $(basename "$ARCHIVE")? [y/N] " yes
[ "$yes" = y ] || [ "$yes" = Y ] || { echo "Nothing changed."; exit 0; }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
tar -xzf "$ARCHIVE" -C "$tmp"
if [ -d "$tmp/mimir-flix" ] && [ ! -d "$tmp/mimir-tv" ]; then  # a backup from before the rename (2026-11)
  mv "$tmp/mimir-flix" "$tmp/mimir-tv"
  [ -f "$tmp/mimir-tv/mimir-flix.db" ] && mv "$tmp/mimir-tv/mimir-flix.db" "$tmp/mimir-tv/mimir-tv.db"
fi
launchctl bootout "gui/$UID/dev.mimir-tv" 2>/dev/null || true
osascript -e 'quit app "Jellyfin"' 2>/dev/null || true
sleep 3

stamp=$(date +%Y%m%d-%H%M%S)
if [ -d "$tmp/mimir-tv" ]; then
  mkdir -p "$DATA" "$CONFIG"
  [ -f "$DATA/mimir-tv.db" ] && mv "$DATA/mimir-tv.db" "$DATA/mimir-tv.db.before-restore-$stamp"
  rm -f "$DATA/mimir-tv.db-wal" "$DATA/mimir-tv.db-shm"
  cp "$tmp/mimir-tv/mimir-tv.db" "$DATA/mimir-tv.db"
  if [ -f "$tmp/mimir-tv/settings.json" ]; then
    [ -f "$CONFIG/settings.json" ] && cp "$CONFIG/settings.json" "$CONFIG/settings.json.before-restore-$stamp"
    cp "$tmp/mimir-tv/settings.json" "$CONFIG/settings.json"
  fi
fi
if [ -d "$tmp/jellyfin" ]; then
  [ -d "$JF" ] && tar -czf "$JF.before-restore-$stamp.tar.gz" -C "$(dirname "$JF")" --exclude jellyfin/metadata \
    --exclude jellyfin/cache --exclude jellyfin/log "$(basename "$JF")"
  mkdir -p "$JF"
  [ -d "$JF/data" ] && find "$JF/data" \( -name '*.db-wal' -o -name '*.db-shm' \) -delete
  cp -R "$tmp/jellyfin/." "$JF/"
fi

open -g /Applications/Jellyfin.app 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/dev.mimir-tv.plist" 2>/dev/null \
  || launchctl kickstart -k "gui/$UID/dev.mimir-tv"
echo "Restored. Open http://127.0.0.1:8723/settings to check what works."
