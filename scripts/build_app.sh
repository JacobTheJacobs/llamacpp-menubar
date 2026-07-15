#!/usr/bin/env bash
# Build a shippable "Llama Menu.app" macOS bundle (no py2app/platypus required).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
APP_NAME="Llama Menu"
BUNDLE_ID="com.llamamenu.app"
DIST="$ROOT/dist"
APP="$DIST/$APP_NAME.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"

echo "==> Building $APP_NAME v$VERSION"

rm -rf "$APP"
mkdir -p "$MACOS" "$RES"

# --- Resources --------------------------------------------------------------
mkdir -p "$RES/resources"
cp "$ROOT/llama_menu.py" "$RES/llama_menu.py"
cp "$ROOT/llama_core.py" "$RES/llama_core.py"
cp "$ROOT/native_app.py" "$RES/native_app.py"
cp "$ROOT/launch_panel.py" "$RES/launch_panel.py"
cp "$ROOT/open_launch_panel.py" "$RES/open_launch_panel.py"
cp "$ROOT/VERSION" "$RES/VERSION"
cp "$ROOT/requirements.txt" "$RES/requirements.txt"
cp "$ROOT/LICENSE" "$RES/LICENSE" 2>/dev/null || true
cp "$ROOT/resources/launch.html" "$RES/launch.html"
cp "$ROOT/resources/launch.html" "$RES/resources/launch.html"
cp "$ROOT/resources/icon.icns" "$RES/AppIcon.icns"
# Menu-bar glyphs: hollow (off) + solid (on) — both required for status cue
cp "$ROOT/resources/"menu_icon*.png "$RES/" 2>/dev/null || true
cp "$ROOT/resources/"menu_icon*.png "$RES/resources/" 2>/dev/null || true
cp "$ROOT/resources/icon.icns" "$RES/resources/icon.icns" 2>/dev/null || true

# --- Native Swift binary ----------------------------------------------------
# The CFBundleExecutable MUST be a real Mach-O binary owned by this app.
# A bash→Python host shows as "Python" to macOS; status items often never appear.
echo "==> Compiling native Swift host…"
swiftc -O \
  -target arm64-apple-macos13.0 \
  -o "$MACOS/Llama Menu" \
  "$ROOT/NativeHost/main.swift"
chmod +x "$MACOS/Llama Menu"
file "$MACOS/Llama Menu"

# --- Info.plist -------------------------------------------------------------
# LSUIElement=true → menu bar only (no Dock icon).
cat > "$CONTENTS/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>Llama Menu</string>
  <key>CFBundleIdentifier</key>
  <string>${BUNDLE_ID}</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>${APP_NAME}</string>
  <key>CFBundleDisplayName</key>
  <string>${APP_NAME}</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>${VERSION}</string>
  <key>CFBundleVersion</key>
  <string>${VERSION}</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.utilities</string>
  <key>NSSupportsAutomaticGraphicsSwitching</key>
  <true/>
  <key>NSHumanReadableCopyright</key>
  <string>Copyright © 2026 Llama Menu. MIT License.</string>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
</dict>
</plist>
PLIST

# PkgInfo
echo -n 'APPL????' > "$CONTENTS/PkgInfo"

# Clear quarantine on build host so open works after copy
xattr -cr "$APP" 2>/dev/null || true

echo "==> Built: $APP"
echo "    Version: $VERSION"
echo "    Bundle ID: $BUNDLE_ID"
du -sh "$APP"
echo ""
echo "Install with:  ./scripts/install.sh"
echo "Or open:       open \"$APP\""
