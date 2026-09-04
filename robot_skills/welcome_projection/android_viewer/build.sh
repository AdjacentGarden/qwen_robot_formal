#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
BUILD_TOOLS_VERSION="${BUILD_TOOLS_VERSION:-36.0.0}"
PLATFORM_VERSION="${PLATFORM_VERSION:-android-36}"
TOOLS="$SDK_ROOT/build-tools/$BUILD_TOOLS_VERSION"
ANDROID_JAR="$SDK_ROOT/platforms/$PLATFORM_VERSION/android.jar"
BUILD="$ROOT/build"
OUT="$ROOT/../assets/welcome-image-viewer.apk"
KEYSTORE="$ROOT/debug.keystore"

if [[ -x /opt/homebrew/opt/openjdk@21/bin/java ]]; then
  export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
  export PATH="$JAVA_HOME/bin:$PATH"
fi

test -f "$ANDROID_JAR"
test -x "$TOOLS/aapt"
test -x "$TOOLS/d8"
test -x "$TOOLS/zipalign"
test -x "$TOOLS/apksigner"
test -s "$ROOT/../assets/welcome_home.png"

rm -rf "$BUILD"
mkdir -p "$BUILD/gen" "$BUILD/classes" "$BUILD/dex" "$BUILD/res/drawable-nodpi"
cp -R "$ROOT/res/." "$BUILD/res/"
cp "$ROOT/../assets/welcome_home.png" "$BUILD/res/drawable-nodpi/welcome_home.png"

"$TOOLS/aapt" package -f -m \
  -J "$BUILD/gen" \
  -M "$ROOT/AndroidManifest.xml" \
  -S "$BUILD/res" \
  -I "$ANDROID_JAR"

javac -source 8 -target 8 -Xlint:-options \
  -bootclasspath "$ANDROID_JAR" \
  -d "$BUILD/classes" \
  "$BUILD/gen/com/adjacentgarden/welcome/R.java" \
  "$ROOT/src/com/adjacentgarden/welcome/WelcomeImageActivity.java"

"$TOOLS/d8" --lib "$ANDROID_JAR" --output "$BUILD/dex" \
  $(find "$BUILD/classes" -name '*.class' -print)

"$TOOLS/aapt" package -f \
  -M "$ROOT/AndroidManifest.xml" \
  -S "$BUILD/res" \
  -I "$ANDROID_JAR" \
  -F "$BUILD/unsigned.apk"

(cd "$BUILD/dex" && "$TOOLS/aapt" add "$BUILD/unsigned.apk" classes.dex >/dev/null)
"$TOOLS/zipalign" -f 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"

if [[ ! -f "$KEYSTORE" ]]; then
  keytool -genkeypair -noprompt \
    -keystore "$KEYSTORE" -storepass android -keypass android \
    -alias androiddebugkey -dname 'CN=Android Debug,O=Android,C=US' \
    -keyalg RSA -keysize 2048 -validity 10000 >/dev/null 2>&1
fi

"$TOOLS/apksigner" sign \
  --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
  --out "$OUT" "$BUILD/aligned.apk"
"$TOOLS/apksigner" verify "$OUT"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$OUT" >"$OUT.sha256"
else
  shasum -a 256 "$OUT" >"$OUT.sha256"
fi
echo "$OUT"
