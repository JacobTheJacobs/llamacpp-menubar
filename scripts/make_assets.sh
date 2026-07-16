#!/usr/bin/env bash
# Regenerate every generated asset from resources/menu-llama-*.svg.
#
# The app icon, the README hero and the menu bar state strip are all derived
# from the same two glyphs, so none of them can drift from what the app ships.
# Panel screenshots are not covered — those need a real model to render.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

echo "==> Rendering…"
swiftc -O -target arm64-apple-macos13.0 \
  -o "$OUT/make_assets" "$ROOT/scripts/make_assets.swift"
"$OUT/make_assets" "$ROOT"

# resources/icon.png is the 1024 master; the .icns is built from it.
echo "==> Building icon.icns…"
ICONSET="$OUT/AppIcon.iconset"
mkdir -p "$ICONSET"
for spec in "16:16x16" "32:16x16@2x" "32:32x32" "64:32x32@2x" \
            "128:128x128" "256:128x128@2x" "256:256x256" "512:256x256@2x" \
            "512:512x512" "1024:512x512@2x"; do
  px="${spec%%:*}"; name="${spec##*:}"
  sips -z "$px" "$px" "$ROOT/resources/icon.png" --out "$ICONSET/icon_$name.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$ROOT/resources/icon.icns"
echo "  icon.icns"

# The README shows the icon at 512.
sips -s format png "$ROOT/resources/icon.icns" \
  --out "$ROOT/docs/assets/app-icon.png" --resampleWidth 512 >/dev/null
echo "  app-icon.png"

echo "==> Done. All artwork derives from resources/menu-llama-*.svg"
