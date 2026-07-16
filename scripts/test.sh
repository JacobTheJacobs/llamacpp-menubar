#!/usr/bin/env bash
# Run the hermetic logic tests (no model files, no network, no UI).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

echo "==> Building tests…"
swiftc -O \
  -target arm64-apple-macos13.0 \
  -o "$OUT/tests" \
  "$ROOT/NativeHost/Util.swift" \
  "$ROOT/NativeHost/ServerProcess.swift" \
  "$ROOT/NativeHost/Config.swift" \
  "$ROOT/NativeHost/Hardware.swift" \
  "$ROOT/NativeHost/GGUF.swift" \
  "$ROOT/NativeHost/Recommender.swift" \
  "$ROOT/Tests/main.swift"

echo "==> Running…"
# Sandbox every write: the suite terminates real child processes, which logs,
# and that must not land in the config dir the installed app is using.
export LLAMA_MENU_CONFIG_DIR="$OUT/config"
"$OUT/tests"
