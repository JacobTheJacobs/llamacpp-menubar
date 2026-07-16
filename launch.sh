#!/usr/bin/env bash
# Dev launcher — prefer the installed .app, else build and run from dist/.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -d "/Applications/Llama Menu.app" ]]; then
  exec open -a "Llama Menu"
fi

if [[ ! -d "$DIR/dist/Llama Menu.app" ]]; then
  "$DIR/scripts/build_app.sh"
fi

exec open "$DIR/dist/Llama Menu.app"
