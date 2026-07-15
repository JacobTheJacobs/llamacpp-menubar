#!/usr/bin/env python3
"""Beautiful per-model launch panel (local browser UI) for Llama Menu.

Opens a polished settings page with recommended defaults for this Mac,
lets the user tweak runtime + sampling params, then starts the server.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

PREFS_PATH = Path.home() / ".config" / "llama-menu" / "model_prefs.json"


def _load_prefs() -> dict:
    try:
        if PREFS_PATH.is_file():
            return json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_prefs(data: dict) -> None:
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def prefs_for_model(path: str) -> dict:
    return dict(_load_prefs().get(path, {}) or {})


def save_prefs_for_model(path: str, params: dict) -> None:
    all_prefs = _load_prefs()
    # Only store launch-relevant keys
    keys = (
        "ctx", "ngl", "threads", "batch", "n_predict", "seed",
        "temperature", "top_p", "top_k", "min_p",
        "repeat_penalty", "repeat_last_n",
    )
    all_prefs[path] = {k: params[k] for k in keys if k in params}
    _save_prefs(all_prefs)


def recommend_params(path: str, ram_gb: float, threads: int, ngl: int = 999, batch: int = 512) -> dict:
    """Best-effort defaults for this Mac (inspired by Llama-macOS context tiers)."""
    try:
        size = os.path.getsize(path) / 1_073_741_824
    except OSError:
        size = 0.0

    free = ram_gb - size - 2.5  # OS reserve
    # Context tiers: pick largest that still leaves headroom
    if free >= 18:
        ctx = 131072
        reason = "Plenty of free RAM — 128K context is comfortable."
    elif free >= 14:
        ctx = 65536
        reason = "Strong headroom — 64K context recommended."
    elif free >= 10:
        ctx = 32768
        reason = "Good balance of speed and context — 32K."
    elif free >= 6:
        ctx = 16384
        reason = "Moderate free RAM — 16K context."
    elif free >= 3:
        ctx = 8192
        reason = "Limited free RAM — 8K context keeps you safe."
    else:
        ctx = 4096
        reason = "Tight memory — 4K context to avoid pressure."

    # Sampling: sensible chat defaults (llama-server defaults, slightly tuned)
    return {
        "ctx": ctx,
        "ngl": int(ngl),
        "threads": int(threads) or 4,
        "batch": int(batch),
        "n_predict": -1,
        "seed": -1,
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 40,
        "min_p": 0.05,
        "repeat_penalty": 1.1,
        "repeat_last_n": 64,
        "_size_gb": round(size, 2),
        "_free_gb": round(max(0.0, free), 1),
        "_reason": reason,
    }


def merge_params(recommended: dict, saved: dict) -> dict:
    out = {k: v for k, v in recommended.items() if not k.startswith("_")}
    for k, v in (saved or {}).items():
        if k in out or k in (
            "ctx", "ngl", "threads", "batch", "n_predict", "seed",
            "temperature", "top_p", "top_k", "min_p",
            "repeat_penalty", "repeat_last_n",
        ):
            out[k] = v
    return out


def _find_html() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [
        here / "resources" / "launch.html",
        here / "launch.html",
        Path(os.environ.get("LLAMA_MENU_RESOURCES", "")) / "launch.html",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError("launch.html not found")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LaunchPanel:
    """One-shot local UI for choosing launch parameters."""

    def __init__(
        self,
        *,
        model_path: str,
        ram_gb: float,
        threads: int,
        ngl: int = 999,
        batch: int = 512,
        on_launch: Optional[Callable[[str, dict], None]] = None,
        on_cancel: Optional[Callable[[], None]] = None,
        open_url: Optional[Callable[[str], None]] = None,
    ):
        self.model_path = model_path
        self.ram_gb = ram_gb
        self.threads = threads
        self.ngl = ngl
        self.batch = batch
        self.on_launch = on_launch
        self.on_cancel = on_cancel
        self.open_url = open_url
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._done = threading.Event()

    def open(self) -> None:
        recommended = recommend_params(
            self.model_path, self.ram_gb, self.threads, self.ngl, self.batch
        )
        saved = prefs_for_model(self.model_path)
        values = merge_params(recommended, saved)

        model_name = os.path.basename(self.model_path).replace(".gguf", "")
        size = recommended.get("_size_gb", 0)
        free = recommended.get("_free_gb", 0)
        meta = f"{size:.1f} GB model · ~{free:.0f} GB free after load"
        reason = recommended.get("_reason", "")

        payload = {
            "model_name": model_name,
            "model_meta": meta,
            "recommend_reason": reason,
            "recommended": {k: v for k, v in recommended.items() if not k.startswith("_")},
            "values": values,
            "model_path": self.model_path,
        }

        html_path = _find_html()
        template = html_path.read_text(encoding="utf-8")
        html = (
            template
            .replace("{{MODEL_NAME}}", _html_esc(model_name))
            .replace("{{MODEL_META}}", _html_esc(meta))
            .replace("{{RAM_GB}}", f"{self.ram_gb:.0f}")
            .replace("{{THREADS}}", str(self.threads))
            .replace("{{DEFAULTS_JSON}}", json.dumps(payload))
        )

        port = _free_port()
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # quiet
                return

            def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._send(200, html.encode("utf-8"))
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    data = {}

                if self.path == "/launch":
                    params = _sanitize_params(data)
                    if data.get("save_defaults"):
                        try:
                            save_prefs_for_model(panel.model_path, params)
                        except Exception:
                            pass
                    self._send(200, b'{"ok":true}', "application/json")
                    # Fire callback off the request thread
                    def go():
                        if panel.on_launch:
                            panel.on_launch(panel.model_path, params)
                        panel.close()
                    threading.Thread(target=go, daemon=True).start()
                    return

                if self.path == "/cancel":
                    self._send(200, b'{"ok":true}', "application/json")
                    def go():
                        if panel.on_cancel:
                            panel.on_cancel()
                        panel.close()
                    threading.Thread(target=go, daemon=True).start()
                    return

                self._send(404, b"not found", "text/plain")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

        url = f"http://127.0.0.1:{port}/"
        if self.open_url:
            self.open_url(url)
        else:
            import subprocess
            subprocess.run(["/usr/bin/open", "-a", "Safari", url], check=False)

        # Auto-stop server after 15 minutes if abandoned
        def watchdog():
            if self._done.wait(900):
                return
            self.close()
        threading.Thread(target=watchdog, daemon=True).start()

    def close(self) -> None:
        self._done.set()
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass


def _html_esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _sanitize_params(data: dict) -> dict:
    def num(key, default, cast=float):
        try:
            return cast(data.get(key, default))
        except (TypeError, ValueError):
            return default

    ctx = int(num("ctx", 8192, float))
    ctx = max(512, min(ctx, 262144))
    ngl = int(num("ngl", 999, float))
    ngl = max(0, min(ngl, 999))
    threads = int(num("threads", 4, float))
    threads = max(1, min(threads, 64))
    batch = int(num("batch", 512, float))
    batch = max(32, min(batch, 8192))
    n_predict = int(num("n_predict", -1, float))
    seed = int(num("seed", -1, float))
    temperature = float(num("temperature", 0.7))
    temperature = max(0.0, min(temperature, 2.0))
    top_p = float(num("top_p", 0.95))
    top_p = max(0.0, min(top_p, 1.0))
    top_k = int(num("top_k", 40, float))
    top_k = max(0, min(top_k, 200))
    min_p = float(num("min_p", 0.05))
    min_p = max(0.0, min(min_p, 1.0))
    repeat_penalty = float(num("repeat_penalty", 1.1))
    repeat_penalty = max(0.5, min(repeat_penalty, 2.0))
    repeat_last_n = int(num("repeat_last_n", 64, float))

    return {
        "ctx": ctx,
        "ngl": ngl,
        "threads": threads,
        "batch": batch,
        "n_predict": n_predict,
        "seed": seed,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "min_p": min_p,
        "repeat_penalty": repeat_penalty,
        "repeat_last_n": repeat_last_n,
    }
