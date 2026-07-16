#!/usr/bin/env bash
# Install Llama Menu.app to /Applications (or ~/Applications) and optional deps.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Llama Menu"
SRC="$ROOT/dist/$APP_NAME.app"
DEST_DIR="${LLAMA_MENU_INSTALL_DIR:-/Applications}"
DEST="$DEST_DIR/$APP_NAME.app"

if [[ ! -d "$SRC" ]]; then
  echo "Building app first..."
  "$ROOT/scripts/build_app.sh"
fi

if [[ ! -d "$SRC" ]]; then
  echo "error: build failed — $SRC missing" >&2
  exit 1
fi

# Check llama-server (soft requirement)
if ! command -v llama-server >/dev/null 2>&1 \
  && [[ ! -x /opt/homebrew/bin/llama-server ]] \
  && [[ ! -x /usr/local/bin/llama-server ]]; then
  echo "warning: llama-server not found. Install with:"
  echo "  brew install llama.cpp"
fi

# Replace existing app
if [[ -d "$DEST" ]]; then
  # Stop running instance via lockfile PID (avoids broad process-name kills)
  LOCK="$HOME/.config/llama-menu/llama-menu.lock"
  if [[ -f "$LOCK" ]]; then
    oldpid="$(cat "$LOCK" 2>/dev/null || true)"
    if [[ -n "${oldpid:-}" ]]; then
      kill "$oldpid" 2>/dev/null || true
      sleep 0.4
    fi
    rm -f "$LOCK"
  fi
  rm -rf "$DEST"
fi

mkdir -p "$DEST_DIR"
cp -R "$SRC" "$DEST"
xattr -cr "$DEST" 2>/dev/null || true

echo "==> Installed: $DEST"

# Start it
open "$DEST"
echo "==> Launched $APP_NAME"
echo ""
echo "Look for 🦙 Llama in the menu bar (top-right)."
echo "Config:  ~/.config/llama-menu/config.json"
echo "Models:  ~/models  (*.gguf)"
echo "Uninstall: ./scripts/uninstall.sh"
