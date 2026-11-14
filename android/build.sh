#!/usr/bin/env bash
# Build the mimir-tv TV app (an Android TV app showing mimir-tv's TV mode) without Gradle:
# aapt2 -> javac -> d8 -> zipalign -> apksigner. Needs JDK 17 and the Android command-line tools (build-tools 34,
# platform 34).
#   ./build.sh                                          one app for any house: it asks for the address on first launch
#   MIMIR_TV_URL=https://lan.home.example.com ./build.sh   the same, starting at that address (no typing)
# Output: out/mimir-tv.apk. Install: see the README (Downloader on the TV, or adb install -r).
set -euo pipefail
cd "$(dirname "$0")"
: "${MIMIR_TV_URL:=}"
SDK=${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}
BT=$SDK/build-tools/34.0.0
JAR=$SDK/platforms/android-34/android.jar
export JAVA_HOME=${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}
export PATH=$JAVA_HOME/bin:$PATH

rm -rf out && mkdir -p out/classes out/gen out/res/values
# A starting address (optional) is built in, not committed: a generated resource next to the committed ones.
cat > out/res/values/url.xml <<EOF
<?xml version="1.0" encoding="utf-8"?>
<resources><string name="mimir_tv_url" translatable="false">${MIMIR_TV_URL}</string></resources>
EOF
# A local signing key, made once and never committed (.gitignore). Updates must be signed with the same key.
if [ ! -f mimir-tv.keystore ]; then
    keytool -genkeypair -keystore mimir-tv.keystore -storepass android -keypass android \
        -alias debug -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=mimir-tv" >/dev/null
fi

"$BT/aapt2" compile --dir res -o out/res.zip
"$BT/aapt2" compile --dir out/res -o out/url.zip
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml -o out/base.apk out/res.zip out/url.zip --java out/gen
javac -nowarn -Xlint:-options -source 8 -target 8 -classpath "$JAR" -d out/classes $(find src out/gen -name '*.java')
"$BT/d8" --lib "$JAR" --min-api 26 --output out $(find out/classes -name '*.class')
cp out/base.apk out/unsigned.apk
(cd out && zip -q unsigned.apk classes.dex)
"$BT/zipalign" -f 4 out/unsigned.apk out/aligned.apk
"$BT/apksigner" sign --ks mimir-tv.keystore --ks-pass pass:android --out out/mimir-tv.apk out/aligned.apk
echo "built $(pwd)/out/mimir-tv.apk"
