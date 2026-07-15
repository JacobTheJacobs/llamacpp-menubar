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

# --- Launcher ---------------------------------------------------------------
# Resolves a Python with rumps, sets up paths, runs as accessory menu-bar app.
cat > "$MACOS/Llama Menu" << 'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RES="$ROOT/Resources"
export LLAMA_MENU_RESOURCES="$RES"
export PYTHONPATH="${RES}${PYTHONPATH:+:$PYTHONPATH}"

# Prefer a Python with PyObjC (AppKit) — required for the menu bar item.
find_python() {
  local candidates=(
    "${LLAMA_MENU_PYTHON:-}"
    "/Library/Developer/CommandLineTools/usr/bin/python3"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "/usr/bin/python3"
  )
  local py
  for py in "${candidates[@]}"; do
    [[ -z "$py" || ! -x "$py" ]] && continue
    if "$py" -c "from AppKit import NSStatusBar; from PyObjCTools import AppHelper" 2>/dev/null; then
      echo "$py"
      return 0
    fi
  done
  for py in "${candidates[@]}"; do
    [[ -n "$py" && -x "$py" ]] && echo "$py" && return 0
  done
  return 1
}

PY="$(find_python)" || {
  osascript -e 'display alert "Llama Menu" message "Python 3 with PyObjC is required.\n\nInstall:\n  python3 -m pip install --user pyobjc-framework-Cocoa"' || true
  exit 1
}

if ! "$PY" -c "from AppKit import NSStatusBar; from PyObjCTools import AppHelper" 2>/dev/null; then
  osascript -e 'display alert "Llama Menu" message "PyObjC is missing.\n\nRun:\n  '"$PY"' -m pip install --user pyobjc-framework-Cocoa"' || true
  exit 1
fi

# Pure AppKit menu bar app (not rumps)
exec "$PY" "$RES/native_app.py"
LAUNCHER
chmod +x "$MACOS/Llama Menu"

# --- Info.plist -------------------------------------------------------------
# LSUIElement=true → menu bar only (no Dock icon), matching Llama-macOS accessory policy.
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
