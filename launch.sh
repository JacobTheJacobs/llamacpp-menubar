#!/bin/bash
# Launch Llama Menu (menu-bar controller for llama-server)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec /Library/Developer/CommandLineTools/usr/bin/python3 "$DIR/llama_menu.py"
