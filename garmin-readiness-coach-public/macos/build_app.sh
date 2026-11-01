#!/bin/bash
# Builds "Readiness Coach for Garmin.app" into ~/Applications (override: ./build_app.sh /other/dir).
# Re-run after moving the project folder or recreating .venv with a different Python.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$HERE")"
DEST_DIR="${1:-$HOME/Applications}"
APP="$DEST_DIR/Readiness Coach for Garmin.app"
EXE="Readiness Coach"
PY="$PROJECT/.venv/bin/python"

[ -x "$PY" ] || { echo "No .venv found at $PROJECT/.venv - create it first." >&2; exit 1; }
"$PY" -c "import webview" || { echo "pywebview missing: $PY -m pip install -r requirements.txt" >&2; exit 1; }

# The Python that .venv was made from, and its embedding flags.
BASE_PY="$("$PY" -c 'import sys; print(sys._base_executable)')"
CONFIG="$("$PY" -c 'import sysconfig; print(sysconfig.get_config_var("BINDIR"))')/python3-config"
[ -x "$CONFIG" ] || CONFIG="$(dirname "$BASE_PY")/python3-config"

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

echo "Compiling launcher against $("$CONFIG" --prefix)"
# shellcheck disable=SC2046
clang -O2 -arch "$(uname -m)" -mmacosx-version-min=11.0 \
  $("$CONFIG" --includes) -DPROJECT_DIR="\"$PROJECT\"" \
  "$HERE/launcher.c" -o "$BUILD/$EXE" \
  $("$CONFIG" --ldflags --embed)

echo "Drawing icon"
"$PY" "$HERE/make_icon.py" "$BUILD/AppIcon.iconset"
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$BUILD/AppIcon.icns"

echo "Assembling $APP"
mkdir -p "$DEST_DIR"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
mv "$BUILD/$EXE" "$APP/Contents/MacOS/"
mv "$BUILD/AppIcon.icns" "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Readiness Coach</string>
  <key>CFBundleDisplayName</key><string>Readiness Coach for Garmin</string>
  <key>CFBundleIdentifier</key><string>local.readiness-coach</string>
  <key>CFBundleExecutable</key><string>$EXE</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSMultipleInstancesProhibited</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

codesign --force --sign - "$APP" >/dev/null 2>&1 || echo "warning: ad-hoc signing failed (app should still run)"
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" || true

echo "Built: $APP"
