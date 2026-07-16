#!/usr/bin/env bash
# Remove Llama Menu.app, LaunchAgent, and optionally config.
set -euo pipefail

APP_NAME="Llama Menu"
BUNDLE_ID="com.llamamenu.app"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/${BUNDLE_ID}.plist"

echo "==> Stopping $APP_NAME"
LOCK="$HOME/.config/llama-menu/llama-menu.lock"
if [[ -f "$LOCK" ]]; then
  oldpid="$(cat "$LOCK" 2>/dev/null || true)"
  if [[ -n "${oldpid:-}" ]]; then
    kill "$oldpid" 2>/dev/null || true
    sleep 0.3
  fi
  rm -f "$LOCK"
fi

if [[ -f "$LAUNCH_AGENT" ]]; then
  launchctl unload "$LAUNCH_AGENT" 2>/dev/null || true
  rm -f "$LAUNCH_AGENT"
  echo "==> Removed LaunchAgent"
fi

for dest in \
  "/Applications/$APP_NAME.app" \
  "$HOME/Applications/$APP_NAME.app"
do
  if [[ -d "$dest" ]]; then
    rm -rf "$dest"
    echo "==> Removed $dest"
  fi
done

if [[ "${1:-}" == "--purge-config" ]]; then
  rm -rf "$HOME/.config/llama-menu"
  echo "==> Purged ~/.config/llama-menu"
else
  echo "(Kept ~/.config/llama-menu — pass --purge-config to remove)"
fi

echo "Done."
