#!/usr/bin/env python3
"""
Llama Menu — entry point.

All UI is pure AppKit (`native_app.py`). Core logic lives in `llama_core.py`.
This file only boots the native app so `python3 llama_menu.py` and the .app
launcher share one path.
"""

from __future__ import annotations


def main() -> None:
    from native_app import run

    run()


if __name__ == "__main__":
    main()
