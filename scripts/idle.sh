#!/bin/bash
# Before restarting mimir-tv: prints "idle" when nothing plays through Jellyfin or mimir-tv (a 60 s check),
# else what plays. Exits 0 only when idle.
L=~/Library/Logs/mimir-tv.log; from=$(wc -l < $L); sleep 60
new=$(tail -n +$((from + 1)) $L | grep -c -e '/hls?' -e '/play?' -e '/jf/Videos' -e '/api/jf/playing')
# The key goes to curl on stdin as a header, never on its command line (which ps shows).
jf=$(printf 'Authorization: MediaBrowser Token="%s"\n' "$(cat ~/.config/mimir-tv/jellyfin-api-key)" | curl -s -H @- \
  'localhost:8096/Sessions?ActiveWithinSeconds=900' | python3 -c 'import json,sys; print(sum(1 for s in json.load(sys.stdin) if s.get("NowPlayingItem")))')
[ "$new" = 0 ] && [ "$jf" = 0 ] && echo idle && exit 0
echo "playing: $new new video requests, $jf Jellyfin sessions playing"; exit 3
