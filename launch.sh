#!/usr/bin/env bash
# Dev launcher — prefer installed .app, else pure AppKit native_app.py
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -d "/Applications/Llama Menu.app" ]]; then
  exec open -a "Llama Menu"
fi
if [[ -d "$DIR/dist/Llama Menu.app" ]]; then
  exec open "$DIR/dist/Llama Menu.app"
fi

export LLAMA_MENU_RESOURCES="${LLAMA_MENU_RESOURCES:-$DIR/resources}"
export PYTHONPATH="${DIR}${PYTHONPATH:+:$PYTHONPATH}"
exec /Library/Developer/CommandLineTools/usr/bin/python3 "$DIR/native_app.py"
