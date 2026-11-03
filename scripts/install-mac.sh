#!/usr/bin/env bash
# Install (or update) mimir-flix on the Mac that runs Jellyfin, and start it at login. On a new Mac, use
# scripts/setup-mac.sh instead: it also sets up Jellyfin and the settings, then runs this.
#   scripts/install-mac.sh            install or update
#   scripts/install-mac.sh --remove   stop and remove (Jellyfin, your files and settings are untouched)
# Needs uv (brew install uv). The Jellyfin API key comes later, from the Setup page.
# Provider plugins (see the README) are installed with it: one per line in ~/.config/mimir-flix/plugins, each a path
# to the plugin's folder or a package spec uv accepts (git+https://…).
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
REPO=$(cd "$(dirname "$0")/.." && pwd)
LABEL=dev.mimir-flix
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI="gui/$(id -u)"

launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--remove" ]; then
  rm -f "$PLIST"
  uv tool uninstall mimir-flix 2>/dev/null || true
  echo "removed"
  exit 0
fi

WITH=()
PLUGINS="${MIMIR_FLIX_CONFIG:-$HOME/.config/mimir-flix}/plugins"
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
    <key>ProgramArguments</key><array><string>$HOME/.local/bin/mimir-flix</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/mimir-flix.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/mimir-flix.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "$GUI" "$PLIST"
# Loading right after the old copy stopped can leave the job loaded but not started: start it explicitly.
launchctl kickstart -k "$GUI/$LABEL"
echo "installed and started (log ~/Library/Logs/mimir-flix.log); set up and check it at http://127.0.0.1:8723/setup and /settings"
