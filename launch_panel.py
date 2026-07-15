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


def detect_hardware() -> dict:
    """Probe this Mac: total/available RAM, perf cores, Apple GPU if any."""
    import re
    import subprocess

    total = 16.0
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
        total = int(out) / (1024**3)
    except Exception:
        pass

    # Available ≈ free + inactive + speculative + purgeable (page size from vm_stat)
    available = total * 0.45  # fallback guess
    try:
        vm = subprocess.check_output(["vm_stat"], text=True)
        page = 4096
        m = re.search(r"page size of (\d+)", vm)
        if m:
            page = int(m.group(1))
        pages = {}
        for line in vm.splitlines():
            mm = re.match(r"\s*(.+?):\s+(\d+)", line)
            if mm:
                pages[mm.group(1).strip().strip('"')] = int(mm.group(2).rstrip("."))
        keys = (
            "Pages free",
            "Pages inactive",
            "Pages speculative",
            "Pages purgeable",
        )
        available = sum(pages.get(k, 0) for k in keys) * page / (1024**3)
        # Compressor pressure: if lots compressed, be slightly less aggressive
        compressed = pages.get("Pages occupied by compressor", 0) * page / (1024**3)
        if compressed > 2:
            available = max(0.5, available - compressed * 0.25)
    except Exception:
        pass

    perf = 0
    logical = os.cpu_count() or 4
    for key in ("hw.perflevel0.logicalcpu", "hw.logicalcpu", "hw.ncpu"):
        try:
            n = int(subprocess.check_output(["sysctl", "-n", key], text=True).strip())
            if n > 0:
                if "perflevel" in key:
                    perf = n
                else:
                    logical = n
                    if not perf:
                        perf = n
                if "perflevel" in key:
                    break
        except Exception:
            continue
    if not perf:
        perf = max(1, logical)

    # Apple GPU core count (Metal) when present
    gpu_cores = 0
    chip = ""
    try:
        brand = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip()
        chip = brand
        sp = subprocess.check_output(
            ["system_profiler", "SPDisplaysDataType"], text=True, timeout=8
        )
        for line in sp.splitlines():
            if "Total Number of Cores" in line:
                gpu_cores = int(re.search(r"(\d+)", line).group(1))
                break
            if "Chipset Model" in line and "Apple" in line:
                chip = line.split(":", 1)[-1].strip()
    except Exception:
        pass

    apple_silicon = "apple" in chip.lower() or bool(
        re.search(r"\bM[0-9]", chip, re.I)
    )

    return {
        "total_ram_gb": round(total, 1),
        "available_ram_gb": round(max(0.5, available), 1),
        "perf_cores": int(perf),
        "logical_cores": int(logical),
        "gpu_cores": int(gpu_cores),
        "chip": chip or "Mac",
        "apple_silicon": apple_silicon,
    }


def _estimate_kv_gb(model_gb: float, ctx: int) -> float:
    """Rough KV + compute buffer estimate for Q4/Q5 GGUF on Metal (1 slot).

    Scales with model size and context. Tuned to stay safe for 7–13B class.
    """
    # Baseline ~0.05–0.12 GiB per 1k tokens, scaled by model weight footprint
    per_1k = 0.055 * max(1.0, model_gb / 3.5)
    kv = per_1k * (ctx / 1000.0)
    compute = 0.35 + model_gb * 0.04  # graph / batch buffers
    return kv + compute


def recommend_params(
    path: str,
    ram_gb: float = 0,
    threads: int = 0,
    ngl: int = 999,
    batch: int = 512,
    hw: dict | None = None,
) -> dict:
    """Pick the *best / maximum safe* launch profile for *this* machine.

    Strategy:
      1. Measure total + currently available RAM, perf cores, GPU.
      2. Budget = min(total − model − OS, available − headroom).
      3. Choose the **largest** context tier that fits that budget (max quality).
      4. Full Metal offload on Apple Silicon; threads = performance cores.
      5. Batch sized for RAM headroom; always 1 parallel slot for chat.
    """
    try:
        size = os.path.getsize(path) / 1_073_741_824
    except OSError:
        size = 0.0

    hw = hw or detect_hardware()
    total = float(ram_gb) if ram_gb and ram_gb > 0 else float(hw["total_ram_gb"])
    available = float(hw.get("available_ram_gb") or total * 0.4)
    perf = int(threads) if threads and threads > 0 else int(hw.get("perf_cores") or 4)
    apple = bool(hw.get("apple_silicon"))
    gpu_cores = int(hw.get("gpu_cores") or 0)
    chip = str(hw.get("chip") or "Mac")

    os_reserve = 2.0 if total <= 16 else 2.5 if total <= 32 else 3.0
    # What we can give the model after weights land
    after_weights = max(0.5, total - size - os_reserve)
    # Don't plan on more than ~90% of *currently* free-ish memory either
    live_budget = max(0.5, available - 1.25)
    budget = min(after_weights, live_budget)
    # If the machine has lots of total RAM but little free right now, still allow
    # a fraction of after_weights (user may free apps) — blend 70/30 toward max
    budget = max(budget, after_weights * 0.55)
    budget = min(budget, after_weights)

    # Context tiers high → low: pick MAX that fits
    tiers = (131072, 65536, 32768, 16384, 8192, 4096)
    ctx = 4096
    for t in tiers:
        if _estimate_kv_gb(size, t) <= budget * 0.92:
            ctx = t
            break

    # Threads: all performance cores (best generation throughput)
    thr = max(1, perf)

    # Batch: larger when we still have headroom after KV estimate
    kv_now = _estimate_kv_gb(size, ctx)
    leftover = max(0.0, budget - kv_now)
    if leftover >= 6:
        batch_sz = 1024
    elif leftover >= 3:
        batch_sz = 512
    elif leftover >= 1.5:
        batch_sz = 256
    else:
        batch_sz = 128
    if batch and batch > 0:
        # Allow caller default only as a soft ceiling when smaller is safer
        batch_sz = min(max(batch_sz, 128), max(int(batch), batch_sz))

    # GPU layers: full offload on Apple Silicon / when Metal present
    ngl_out = 999 if apple or gpu_cores > 0 else max(0, int(ngl) if ngl else 0)
    if not apple and ngl and 0 < int(ngl) < 999:
        ngl_out = int(ngl)

    # Sampling: high-quality chat defaults (not related to RAM)
    temp, top_p, top_k, min_p = 0.7, 0.95, 40, 0.05
    if size >= 20:  # big models: slightly cooler
        temp = 0.65

    ctx_k = ctx // 1024
    reason = (
        f"Max for this Mac ({chip}): {total:.0f} GB RAM, ~{available:.0f} GB free now, "
        f"{thr} perf threads"
        + (f", {gpu_cores} GPU cores" if gpu_cores else "")
        + f" → ctx {ctx_k}K, ngl {ngl_out}, batch {batch_sz} "
        f"(fits ~{budget:.1f} GB after a {size:.1f} GB model)."
    )

    return {
        "ctx": ctx,
        "ngl": ngl_out,
        "threads": thr,
        "batch": batch_sz,
        "n_predict": -1,
        "seed": -1,
        "temperature": temp,
        "top_p": top_p,
        "top_k": top_k,
        "min_p": min_p,
        "repeat_penalty": 1.1,
        "repeat_last_n": 64,
        "parallel": 1,
        "_size_gb": round(size, 2),
        "_free_gb": round(max(0.0, available), 1),
        "_budget_gb": round(budget, 1),
        "_total_ram_gb": round(total, 1),
        "_chip": chip,
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
        hw = detect_hardware()
        recommended = recommend_params(
            self.model_path,
            self.ram_gb or hw["total_ram_gb"],
            self.threads or hw["perf_cores"],
            self.ngl,
            self.batch,
            hw=hw,
        )
        saved = prefs_for_model(self.model_path)
        # Saved prefs win for user tweaks, but never above freshly computed max ctx
        values = merge_params(recommended, saved)
        max_ctx = int(recommended.get("ctx") or 4096)
        try:
            if int(values.get("ctx") or 0) > max_ctx:
                values["ctx"] = max_ctx
        except Exception:
            values["ctx"] = max_ctx

        model_name = os.path.basename(self.model_path).replace(".gguf", "")
        size = recommended.get("_size_gb", 0)
        free = recommended.get("_free_gb", 0)
        budget = recommended.get("_budget_gb", free)
        chip = recommended.get("_chip", "Mac")
        mmproj = None
        try:
            from llama_core import find_mmproj

            mmproj = find_mmproj(self.model_path)
        except Exception:
            mmproj = None
        meta = (
            f"{size:.1f} GB model · {chip} · "
            f"{recommended.get('_total_ram_gb', self.ram_gb):.0f} GB RAM · "
            f"~{free:.0f} GB free · budget {budget:.1f} GB"
        )
        if mmproj:
            meta += f" · vision ({os.path.basename(mmproj)})"
        reason = recommended.get("_reason", "")
        if mmproj:
            reason += " Multimodal projector found — will pass --mmproj automatically."

        rec_clean = {k: v for k, v in recommended.items() if not k.startswith("_")}
        if mmproj:
            rec_clean["mmproj"] = mmproj
            values["mmproj"] = mmproj

        payload = {
            "model_name": model_name,
            "model_meta": meta,
            "recommend_reason": reason,
            "recommended": rec_clean,
            "values": values,
            "model_path": self.model_path,
            "mmproj": mmproj or "",
            "hardware": {
                "chip": chip,
                "total_ram_gb": recommended.get("_total_ram_gb"),
                "available_ram_gb": free,
                "budget_gb": budget,
                "perf_cores": recommended.get("threads"),
            },
        }

        html_path = _find_html()
        template = html_path.read_text(encoding="utf-8")
        html = (
            template
            .replace("{{MODEL_NAME}}", _html_esc(model_name))
            .replace("{{MODEL_META}}", _html_esc(meta))
            .replace("{{RAM_GB}}", f"{recommended.get('_total_ram_gb', self.ram_gb):.0f}")
            .replace("{{THREADS}}", str(recommended.get("threads", self.threads)))
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

    parallel = int(num("parallel", 1, float))
    parallel = max(1, min(parallel, 4))

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
        "parallel": parallel,
    }
