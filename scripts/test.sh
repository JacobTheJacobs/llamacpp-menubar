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
  "$ROOT/NativeHost/Config.swift" \
  "$ROOT/NativeHost/Hardware.swift" \
  "$ROOT/NativeHost/GGUF.swift" \
  "$ROOT/NativeHost/Recommender.swift" \
  "$ROOT/Tests/main.swift"

echo "==> Running…"
"$OUT/tests"
