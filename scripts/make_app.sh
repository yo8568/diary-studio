#!/usr/bin/env bash
# Build "Diary Studio.app".
#
# The bundle is a launcher, not a frozen copy: mlx, torch and resemblyzer are
# hundreds of megabytes and mlx needs the real Metal stack, so freezing them
# buys nothing on the one Mac this runs on. The .app points at the repo and its
# virtualenv, which also means editing the code updates the app immediately.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${DIARY_VENV:-$HOME/.venvs/whisper}"
DEST="${1:-$HOME/Applications}"
APP="$DEST/Diary Studio.app"

[ -x "$VENV/bin/python" ] || { echo "找不到 venv：$VENV" >&2; exit 1; }

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Diary Studio</string>
  <key>CFBundleDisplayName</key><string>Diary Studio</string>
  <key>CFBundleIdentifier</key><string>io.github.yo8568.diary-studio</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>diary-studio</string>
  <key>CFBundleIconFile</key><string>app.icns</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/diary-studio" <<LAUNCH
#!/bin/bash
# Finder gives GUI apps a minimal PATH; ffmpeg lives in Homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
cd "$REPO"
exec "$VENV/bin/python" -m diary "\$@" >> "\$HOME/Library/Logs/diary-studio.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/diary-studio"

if [ -f "$REPO/assets/app.icns" ]; then
  cp "$REPO/assets/app.icns" "$APP/Contents/Resources/app.icns"
fi

echo "建好了：$APP"
echo "紀錄檔：~/Library/Logs/diary-studio.log"
