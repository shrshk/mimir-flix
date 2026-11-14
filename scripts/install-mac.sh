#!/usr/bin/env bash
# Install (or update) mimir-tv on the Mac that runs Jellyfin, and start it at login. On a new Mac, use
# scripts/setup-mac.sh instead: it also sets up Jellyfin and the settings, then runs this.
#   scripts/install-mac.sh            install or update
#   scripts/install-mac.sh --remove   stop and remove (Jellyfin, your files and settings are untouched)
# Needs uv (brew install uv). The Jellyfin API key comes later, from the Setup page.
# Provider plugins (see the README) are installed with it: one per line in ~/.config/mimir-tv/plugins, each a path
# to the plugin's folder or a package spec uv accepts (git+https://…).
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
REPO=$(cd "$(dirname "$0")/.." && pwd)
LABEL=dev.mimir-tv
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI="gui/$(id -u)"

launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--remove" ]; then
  rm -f "$PLIST"
  uv tool uninstall mimir-tv 2>/dev/null || true
  echo "removed"
  exit 0
fi

# Renamed from mimir-flix (2026-11): its background job, app, folders and database move over once. The old job's
# plist is kept as .bak; the vault's Keychain item keeps its old name, so saved logins stay readable.
if [ -f "$HOME/Library/LaunchAgents/dev.mimir-flix.plist" ]; then
  launchctl bootout "$GUI/dev.mimir-flix" 2>/dev/null || true
  mv "$HOME/Library/LaunchAgents/dev.mimir-flix.plist" "$HOME/Library/LaunchAgents/dev.mimir-flix.plist.bak"
  uv tool uninstall mimir-flix 2>/dev/null || true
fi
for dir in "$HOME/.config" "$HOME/Library/Application Support"; do
  if [ -d "$dir/mimir-flix" ] && [ ! -e "$dir/mimir-tv" ]; then mv "$dir/mimir-flix" "$dir/mimir-tv"; fi
done
DATA="$HOME/Library/Application Support/mimir-tv"
for end in "" -wal -shm; do
  if [ -f "$DATA/mimir-flix.db$end" ] && [ ! -e "$DATA/mimir-tv.db$end" ]; then mv "$DATA/mimir-flix.db$end" "$DATA/mimir-tv.db$end"; fi
done
if [ -f "$DATA/tv/mimir-flix-tv.apk" ] && [ ! -e "$DATA/tv/mimir-tv.apk" ]; then mv "$DATA/tv/mimir-flix-tv.apk" "$DATA/tv/mimir-tv.apk"; fi

WITH=()
PLUGINS="${MIMIR_TV_CONFIG:-$HOME/.config/mimir-tv}/plugins"
if [ -f "$PLUGINS" ]; then
  while IFS= read -r p; do
    case "$p" in ""|"#"*) continue ;; esac
    WITH+=(--with "${p/#\~/$HOME}")
  done < "$PLUGINS"
fi
uv tool install --force --quiet ${WITH[@]+"${WITH[@]}"} "$REPO/service"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key><array><string>$HOME/.local/bin/mimir-tv</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/mimir-tv.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/mimir-tv.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "$GUI" "$PLIST"
# Loading right after the old copy stopped can leave the job loaded but not started: start it explicitly.
launchctl kickstart -k "$GUI/$LABEL"
echo "installed and started (log ~/Library/Logs/mimir-tv.log); set up and check it at http://127.0.0.1:8723/setup and /settings"
