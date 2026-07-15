#!/usr/bin/env python3
"""
CLI bridge: open the recommended-settings panel, write result JSON, exit.

Usage:
  open_launch_panel.py <model.gguf> <result.json> [ram_gb] [threads] [ngl] [batch]

Result JSON:
  {"ok": true, "path": "...", "params": {...}}
  {"ok": false, "cancelled": true}
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from launch_panel import LaunchPanel  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: open_launch_panel.py model.gguf result.json", file=sys.stderr)
        return 2

    model = os.path.abspath(sys.argv[1])
    out = Path(sys.argv[2])
    ram = float(sys.argv[3]) if len(sys.argv) > 3 else 16.0
    threads = int(sys.argv[4]) if len(sys.argv) > 4 else 8
    ngl = int(sys.argv[5]) if len(sys.argv) > 5 else 999
    batch = int(sys.argv[6]) if len(sys.argv) > 6 else 512

    if not os.path.isfile(model):
        out.write_text(json.dumps({"ok": False, "error": "model missing"}) + "\n")
        return 1

    done = threading.Event()
    result: dict = {"ok": False, "cancelled": True}

    def on_launch(path: str, params: dict) -> None:
        nonlocal result
        result = {"ok": True, "path": path, "params": params}
        done.set()

    def on_cancel() -> None:
        nonlocal result
        result = {"ok": False, "cancelled": True}
        done.set()

    def open_url(url: str) -> None:
        import subprocess

        for app in ("Safari", "Google Chrome", "Arc", "Firefox"):
            r = subprocess.run(
                ["/usr/bin/open", "-a", app, url],
                capture_output=True,
            )
            if r.returncode == 0:
                return
        subprocess.run(["/usr/bin/open", url], check=False)

    panel = LaunchPanel(
        model_path=model,
        ram_gb=ram,
        threads=threads,
        ngl=ngl,
        batch=batch,
        on_launch=on_launch,
        on_cancel=on_cancel,
        open_url=open_url,
    )
    panel.open()

    # Wait until user starts or cancels (max 15 min)
    if not done.wait(timeout=900):
        result = {"ok": False, "cancelled": True, "error": "timeout"}
        try:
            panel.close()
        except Exception:
            pass

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    # Give browser a moment to finish fetch
    import time

    time.sleep(0.3)
    try:
        panel.close()
    except Exception:
        pass
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
