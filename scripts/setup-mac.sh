#!/usr/bin/env bash
# One command for a new Mac: Jellyfin and mimir-flix, started at login, then setup in the browser.
#   git clone <this repo> && cd mimir-flix && scripts/setup-mac.sh
# Safe to run again: it keeps what is already set up. Needs Homebrew (https://brew.sh). It asks before installing
# anything (the Setup page, /setup, shows the same list in the browser).
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
REPO=$(cd "$(dirname "$0")/.." && pwd)

# ask "<what it is for>" <brew args…>: installs only on a yes.
ask() {
  local why=$1; shift
  read -r -p "Install $why (brew install $*)? [y/N] " yes
  [ "$yes" = y ] || [ "$yes" = Y ] || { echo "Skipped: mimir-flix needs it; run this again when ready." >&2; exit 1; }
  brew install "$@"
}
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh" >&2; exit 1; }
command -v uv >/dev/null || ask "uv, which runs mimir-flix" uv
[ -d /Applications/Jellyfin.app ] || ask "Jellyfin, the media server mimir-flix works with" --cask jellyfin
# Jellyfin starts at login and now.
osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/Jellyfin.app", hidden:true}' >/dev/null 2>&1 || true
pgrep -qf "Jellyfin.app" || open -g /Applications/Jellyfin.app

uv tool install --force --quiet "$REPO/service"
"$REPO/scripts/install-mac.sh"
sleep 4
# The rest is in the browser: Jellyfin, your media, your apps, the TV and phones, one step at a time.
open "http://127.0.0.1:8723/setup"
