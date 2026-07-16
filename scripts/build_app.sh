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
cp "$ROOT/VERSION" "$RES/VERSION"
cp "$ROOT/LICENSE" "$RES/LICENSE" 2>/dev/null || true
cp "$ROOT/resources/launch.html" "$RES/launch.html"
cp "$ROOT/resources/settings.html" "$RES/settings.html"
cp "$ROOT/resources/panel.css" "$RES/panel.css"
# Menu bar glyphs: hollow (off) + solid (on). SVG loads straight into NSImage,
# so there is no rasterisation step and no @2x variants to keep in sync.
cp "$ROOT/resources/menu-llama-off.svg" "$RES/menu-llama-off.svg"
cp "$ROOT/resources/menu-llama-on.svg" "$RES/menu-llama-on.svg"
cp "$ROOT/resources/icon.icns" "$RES/AppIcon.icns"

# --- Native Swift binary ----------------------------------------------------
# The CFBundleExecutable MUST be a real Mach-O binary owned by this app.
# A bash→Python host shows as "Python" to macOS; status items often never appear.
echo "==> Compiling native Swift host…"
# main.swift last: top-level code is only legal in that file, and swiftc wants
# it named main.swift, but ordering keeps the intent obvious.
swiftc -O \
  -target arm64-apple-macos13.0 \
  -o "$MACOS/Llama Menu" \
  "$ROOT/NativeHost/Util.swift" \
  "$ROOT/NativeHost/Config.swift" \
  "$ROOT/NativeHost/Hardware.swift" \
  "$ROOT/NativeHost/GGUF.swift" \
  "$ROOT/NativeHost/Recommender.swift" \
  "$ROOT/NativeHost/LaunchAtLogin.swift" \
  "$ROOT/NativeHost/WebPanel.swift" \
  "$ROOT/NativeHost/LaunchPanel.swift" \
  "$ROOT/NativeHost/SettingsPanel.swift" \
  "$ROOT/NativeHost/AppController.swift" \
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
