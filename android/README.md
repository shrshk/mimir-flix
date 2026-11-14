# mimir-tv on the TV

The Android TV app for [mimir-tv](../README.md): mimir-tv's TV mode full screen,
driven with the remote (search, browse, ▶ plays in the TV's Jellyfin app), and messages from the phone (a doodle or a
photo) shown over whatever is on. One app for any house: it asks where mimir-tv is the first time it opens.

Works on Android TV and Google TV (Sony, TCL, Chromecast with Google TV …) and Fire TV (Fire Stick, Fire TV Cube).

## Install

You need mimir-tv running on a Mac in the same home network, and the APK (`mimir-tv.apk`): mimir-tv
serves it at `<mimir-tv address>/tv/app.apk` once it is built (see Build).

### On the TV, with the Downloader app (no computer needed)

1. On the TV, allow apps from unknown sources:
   - Android TV / Google TV: Settings → Device Preferences (or System) → About → click Build 7 times to turn on
     developer mode; then Settings → Apps → Security & restrictions → Unknown sources → allow Downloader (step 2).
   - Fire TV: Settings → My Fire TV → Developer options → Install unknown apps → Downloader: On (if Developer options
     is missing: My Fire TV → About → click the device name 7 times).
2. Install **Downloader** (by AFTVnews) from the TV's app store.
3. Open Downloader, type the address mimir-tv's Settings page shows on its **TV app** card, for example
   `http://192.168.1.20:8723/tv/app.apk`, and choose Install.
4. Open **mimir-tv** from the TV's apps. The first time it asks where mimir-tv is: type the same address without
   `/tv/app.apk` (e.g. `192.168.1.20:8723`), then Connect.

### With a computer (adb)

1. Turn on developer mode and USB/network debugging on the TV (Settings → Device Preferences → Developer options).
2. `adb connect <tv-ip>:5555`, accept the prompt on the TV, then `adb install -r mimir-tv.apk`.

To change the address later: when mimir-tv is not answering the app says so; press OK to type another one.
With a domain of your own the address can be a name, such as `https://lan.home.example.com`
([Your own domain with Cloudflare](https://gist.github.com/shrshk/58b710264907d1e229675a2313f24b0e)).

### Show on TV (optional)

Messages from the phone draw over other apps, which Android allows only when told to, once:
`adb shell appops set dev.mimirflix.tv SYSTEM_ALERT_WINDOW allow`. mimir-tv starts the overlay over the TV's
network debugging (Settings → TVs → Pair on mimir-tv's Settings page).

## Build

No Gradle: JDK 17 and the Android command-line tools (build-tools 34, platform 34).

```sh
brew install openjdk@17 android-commandlinetools
sdkmanager "build-tools;34.0.0" "platforms;android-34"
./build.sh                                        # out/mimir-tv.apk, asks for the address on first launch
MIMIR_TV_URL=https://lan.example.com ./build.sh  # the same, starting at that address
cp out/mimir-tv.apk "$HOME/Library/Application Support/mimir-tv/tv/"   # mimir-tv serves it at /tv/app.apk
```

The first build makes a signing key (`mimir-tv.keystore`, never committed). Keep it: an update installs over
the app only when it is signed with the same key.

Before the rename to mimir-tv this app was its own repo (mimir-flix-tv). Its Android id stays `dev.mimirflix.tv`, as
a new id would be a new app (installed beside the old one, its address typed again). A key from then is called
`mimir-flix-tv.keystore`: rename it to `mimir-tv.keystore` here.
