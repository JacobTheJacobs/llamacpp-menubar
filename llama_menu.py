#!/usr/bin/env python3
"""
llama_menu — production-oriented macOS menu bar controller for llama-server.

Lightweight rumps app inspired by Llama-macOS / llama-server-osx UX patterns:
status states, health checks, config, logs, and a clear menu hierarchy.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import rumps

try:
    from AppKit import NSApplication, NSWorkspace
except Exception:  # pragma: no cover
    NSApplication = None
    NSWorkspace = None

# ---------------------------------------------------------------------------
# Paths & defaults
# ---------------------------------------------------------------------------

APP_NAME = "Llama Menu"
APP_VERSION = "1.1.0"
DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"
DEFAULT_MODELS_DIR = Path.home() / "models"
CONFIG_DIR = Path.home() / ".config" / "llama-menu"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_DIR = CONFIG_DIR / "logs"
LOG_PATH = LOG_DIR / "server.log"
HEALTH_TIMEOUT_S = 0.4
READY_POLL_INTERVAL_S = 0.5
READY_TIMEOUT_S = 120.0
TICK_INTERVAL_S = 2.0
OS_RESERVE_GB = 2.5


# ---------------------------------------------------------------------------
# System helpers
# ---------------------------------------------------------------------------

def _set_accessory_activation_policy() -> None:
    """Keep the app menu-bar-only (no Dock icon)."""
    if NSApplication is None:
        return
    try:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
    except Exception:
        pass


def total_ram_gb() -> float:
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
        return int(out) / (1024**3)
    except Exception:
        return 16.0


def perf_core_count() -> int:
    """Prefer performance-core count on Apple Silicon; fall back to logical CPUs."""
    for key in ("hw.perflevel0.logicalcpu", "hw.logicalcpu", "hw.ncpu"):
        try:
            out = subprocess.check_output(["sysctl", "-n", key], text=True).strip()
            n = int(out)
            if n > 0:
                return n
        except Exception:
            continue
    return max(1, os.cpu_count() or 4)


def discover_llama_server() -> Optional[str]:
    """Find llama-server binary (PATH, Homebrew, common locations)."""
    which = shutil.which("llama-server")
    if which and os.access(which, os.X_OK):
        return which

    candidates = [
        "/opt/homebrew/bin/llama-server",
        "/usr/local/bin/llama-server",
        str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-server"),
        str(Path.home() / ".local" / "bin" / "llama-server"),
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def open_path(path: str | Path) -> None:
    path = str(path)
    if NSWorkspace is not None:
        try:
            NSWorkspace.sharedWorkspace().openFile_(path)
            return
        except Exception:
            pass
    subprocess.run(["open", path], check=False)


def open_url(url: str) -> None:
    if NSWorkspace is not None:
        try:
            from Foundation import NSURL

            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
            return
        except Exception:
            pass
    subprocess.run(["open", url], check=False)


def copy_to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode(), check=False)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    llama_server: str = ""
    models_dir: str = str(DEFAULT_MODELS_DIR)
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    ngl: int = 999
    batch: int = 512
    threads: int = 0  # 0 = auto (perf cores)
    auto_ctx: bool = True
    fixed_ctx: int = 8192
    launch_at_login: bool = False

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        if CONFIG_PATH.is_file():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                for key in cfg.__dict__:
                    if key in data:
                        setattr(cfg, key, data[key])
            except Exception:
                pass
        if not cfg.llama_server:
            cfg.llama_server = discover_llama_server() or ""
        if not cfg.threads:
            cfg.threads = perf_core_count()
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: getattr(self, k) for k in self.__dict__}
        CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def ensure_dirs(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        models = Path(self.models_dir).expanduser()
        models.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Model sizing / labels
# ---------------------------------------------------------------------------

def model_size_gb(path: str) -> float:
    try:
        return os.path.getsize(path) / 1_073_741_824
    except OSError:
        return 0.0


def optimal_params(path: str, cfg: AppConfig, ram_gb: float) -> dict:
    size = model_size_gb(path)
    if cfg.auto_ctx:
        free = ram_gb - size - OS_RESERVE_GB
        if free >= 14:
            ctx = 65536
        elif free >= 10:
            ctx = 32768
        elif free >= 6:
            ctx = 16384
        elif free >= 3:
            ctx = 8192
        else:
            ctx = 4096
    else:
        ctx = int(cfg.fixed_ctx)

    return {
        "ngl": int(cfg.ngl),
        "ctx": ctx,
        "threads": int(cfg.threads) or perf_core_count(),
        "batch": int(cfg.batch),
    }


def short_name(path: str, limit: int = 42) -> str:
    name = os.path.basename(path).replace(".gguf", "")
    return name[: limit - 1] + "…" if len(name) > limit else name


def fmt_ctx(ctx: int) -> str:
    if ctx >= 1024:
        return f"{ctx // 1024}K"
    return str(ctx)


def fmt_size(gb: float) -> str:
    if gb >= 10:
        return f"{gb:.0f} GB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{gb * 1024:.0f} MB"


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

class ServerState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class Runtime:
    state: ServerState = ServerState.STOPPED
    proc: Optional[subprocess.Popen] = None
    model_path: Optional[str] = None
    params: dict = field(default_factory=dict)
    error_message: str = ""
    started_at: float = 0.0


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class LlamaCppApp(rumps.App):
    def __init__(self):
        _set_accessory_activation_policy()
        super().__init__("Llama", quit_button=None)

        self.cfg = AppConfig.load()
        self.cfg.ensure_dirs()
        self.cfg.save()  # write defaults on first run

        self.ram_gb = total_ram_gb()
        self._lock = threading.RLock()
        self.rt = Runtime()
        self._log_fp = None
        self._ready_thread: Optional[threading.Thread] = None

        self.rebuild_menu()
        rumps.Timer(self.tick, TICK_INTERVAL_S).start()

    # -- URLs ----------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.cfg.host}:{self.cfg.port}"

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/v1"

    @property
    def webui_url(self) -> str:
        return f"{self.base_url}/"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"

    # -- Process / health ----------------------------------------------------

    def _pgrep_server(self) -> bool:
        r = subprocess.run(
            ["pgrep", "-a", "-f", f"llama-server.*--port {self.cfg.port}"],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0 and bool(r.stdout.strip())

    def _port_listening(self) -> bool:
        r = subprocess.run(
            ["lsof", "-i", f":{self.cfg.port}", "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True,
            text=True,
        )
        return "llama" in r.stdout.lower()

    def process_alive(self) -> bool:
        if self.rt.proc is not None:
            if self.rt.proc.poll() is None:
                return True
            return False
        return self._pgrep_server() or self._port_listening()

    def health_ok(self) -> bool:
        try:
            req = urllib.request.Request(self.health_url, method="GET")
            with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_S) as resp:
                return 200 <= resp.status < 300
        except Exception:
            # Some builds expose /v1/models but not /health
            try:
                req = urllib.request.Request(f"{self.api_url}/models", method="GET")
                with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_S) as resp:
                    return 200 <= resp.status < 300
            except Exception:
                return False

    def _find_running_model(self) -> Optional[str]:
        r = subprocess.run(
            ["pgrep", "-a", "-f", "llama-server"],
            capture_output=True,
            text=True,
        )
        for line in r.stdout.splitlines():
            if "--model" not in line:
                continue
            parts = line.split("--model", 1)
            model_arg = parts[1].strip()
            if model_arg:
                return model_arg.split()[0].strip()
        return None

    def _close_log(self) -> None:
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None

    def _kill_existing_server(self) -> None:
        proc = self.rt.proc
        self.rt.proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        # Targeted cleanup for orphaned processes on our port
        subprocess.run(
            ["pkill", "-f", f"llama-server.*--port {self.cfg.port}"],
            capture_output=True,
        )
        # Brief wait so the port frees
        for _ in range(20):
            if not self._port_listening() and not self._pgrep_server():
                break
            time.sleep(0.1)
        self._close_log()

    def _sync_external_state(self) -> bool:
        """Adopt externally started server or clear dead state. Returns if menu should rebuild."""
        changed = False
        alive = self.process_alive()
        healthy = self.health_ok() if alive else False

        if self.rt.state == ServerState.STARTING:
            if not alive:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = "Server exited before becoming ready"
                self.rt.model_path = None
                self.rt.params = {}
                self.rt.proc = None
                self._close_log()
                changed = True
            elif healthy:
                self.rt.state = ServerState.RUNNING
                changed = True
            elif time.time() - self.rt.started_at > READY_TIMEOUT_S:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = "Timed out waiting for server readiness"
                self._kill_existing_server()
                changed = True
            return changed

        if self.rt.state == ServerState.RUNNING:
            if not alive:
                self.rt.state = ServerState.STOPPED
                self.rt.model_path = None
                self.rt.params = {}
                self.rt.proc = None
                self.rt.error_message = ""
                self._close_log()
                changed = True
            return changed

        if self.rt.state in (ServerState.STOPPED, ServerState.ERROR):
            if alive and healthy:
                self.rt.state = ServerState.RUNNING
                if not self.rt.model_path:
                    self.rt.model_path = self._find_running_model()
                changed = True
            elif alive and not healthy and self.rt.state != ServerState.STARTING:
                # Process up but not healthy yet — treat as starting if we didn't launch it
                pass
        return changed

    def tick(self, _):
        with self._lock:
            changed = self._sync_external_state()
            if changed or True:
                # Always refresh title/menu cheaply; rebuild is light for typical model counts
                self._do_rebuild()

    # -- Menu construction ---------------------------------------------------

    def rebuild_menu(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._do_rebuild()
        finally:
            self._lock.release()

    def _status_title(self) -> str:
        state = self.rt.state
        if state == ServerState.RUNNING:
            return "Llama ●"
        if state == ServerState.STARTING:
            return "Llama ◐"
        if state == ServerState.ERROR:
            return "Llama !"
        return "Llama"

    def _do_rebuild(self):
        self.title = self._status_title()
        self.menu.clear()

        state = self.rt.state
        items: list = []

        # Header / status
        if state == ServerState.RUNNING:
            p = self.rt.params
            model = short_name(self.rt.model_path or "unknown")
            items.extend(
                [
                    rumps.MenuItem("Status  Running", callback=None),
                    rumps.MenuItem(f"Model   {model}", callback=None),
                    rumps.MenuItem(
                        f"Params  ctx {fmt_ctx(p.get('ctx', 0))} · "
                        f"ngl {p.get('ngl', '—')} · "
                        f"{p.get('threads', '—')}T · "
                        f"batch {p.get('batch', '—')}",
                        callback=None,
                    ),
                    rumps.MenuItem(f"API     {self.cfg.host}:{self.cfg.port}/v1", callback=None),
                    None,
                    rumps.MenuItem("Copy API URL", callback=self.copy_url),
                    rumps.MenuItem("Open WebUI", callback=self.open_webui),
                    None,
                    self._model_submenu("Switch Model", self.start_server),
                    rumps.MenuItem("Stop Server", callback=self.stop_server),
                ]
            )
        elif state == ServerState.STARTING:
            model = short_name(self.rt.model_path or "…")
            items.extend(
                [
                    rumps.MenuItem("Status  Starting…", callback=None),
                    rumps.MenuItem(f"Model   {model}", callback=None),
                    rumps.MenuItem("Loading weights — usually 5–30s", callback=None),
                    None,
                    rumps.MenuItem("Cancel / Stop", callback=self.stop_server),
                ]
            )
        elif state == ServerState.ERROR:
            err = self.rt.error_message or "Unknown error"
            if len(err) > 60:
                err = err[:57] + "…"
            items.extend(
                [
                    rumps.MenuItem("Status  Error", callback=None),
                    rumps.MenuItem(f"  {err}", callback=None),
                    None,
                    self._model_submenu("Retry with Model", self.start_server),
                    rumps.MenuItem("Open Server Log", callback=self.open_log),
                ]
            )
        else:
            binary_ok = bool(self.cfg.llama_server and os.access(self.cfg.llama_server, os.X_OK))
            items.append(rumps.MenuItem("Status  Stopped", callback=None))
            if not binary_ok:
                items.append(rumps.MenuItem("llama-server not found", callback=None))
                items.append(rumps.MenuItem("Install via: brew install llama.cpp", callback=None))
            items.append(None)
            items.append(self._model_submenu("Start Server", self.start_server))

        # Utilities
        items.extend(
            [
                None,
                self._tools_menu(),
                self._settings_info_menu(),
                None,
                rumps.MenuItem("Quit Llama Menu", callback=rumps.quit_application),
            ]
        )

        self.menu = items

    def _tools_menu(self) -> rumps.MenuItem:
        parent = rumps.MenuItem("Tools")
        parent.add(rumps.MenuItem("Open Models Folder", callback=self.open_models_folder))
        parent.add(rumps.MenuItem("Reveal Config", callback=self.open_config_folder))
        parent.add(rumps.MenuItem("Open Server Log", callback=self.open_log))
        parent.add(rumps.MenuItem("Refresh Model List", callback=self.refresh_models))
        if self.rt.state == ServerState.RUNNING:
            parent.add(None)
            parent.add(rumps.MenuItem("Copy API URL", callback=self.copy_url))
            parent.add(rumps.MenuItem("Open WebUI", callback=self.open_webui))
        return parent

    def _settings_info_menu(self) -> rumps.MenuItem:
        parent = rumps.MenuItem("Settings")
        parent.add(
            rumps.MenuItem(
                f"Port  {self.cfg.port}",
                callback=None,
            )
        )
        parent.add(
            rumps.MenuItem(
                f"Models  {self.cfg.models_dir}",
                callback=None,
            )
        )
        binary = self.cfg.llama_server or "(not found)"
        if len(binary) > 48:
            binary = "…" + binary[-47:]
        parent.add(rumps.MenuItem(f"Binary  {binary}", callback=None))
        parent.add(
            rumps.MenuItem(
                f"Hardware  {self.ram_gb:.0f} GB RAM · {self.cfg.threads} threads",
                callback=None,
            )
        )
        parent.add(None)
        parent.add(rumps.MenuItem("Edit Config…", callback=self.edit_config))
        parent.add(rumps.MenuItem("Reload Config", callback=self.reload_config))
        parent.add(None)
        parent.add(rumps.MenuItem(f"Llama Menu  v{APP_VERSION}", callback=None))
        parent.add(
            rumps.MenuItem(
                f"Python  {platform.python_version()} · {platform.machine()}",
                callback=None,
            )
        )
        return parent

    def _model_submenu(self, label: str, callback: Callable) -> rumps.MenuItem:
        parent = rumps.MenuItem(label)
        models_dir = Path(self.cfg.models_dir).expanduser()

        if not models_dir.is_dir():
            parent.add(rumps.MenuItem(f"Models folder missing: {models_dir}", callback=None))
            parent.add(rumps.MenuItem("Open / Create Folder", callback=self.open_models_folder))
            return parent

        files = sorted(
            set(
                glob.glob(str(models_dir / "**" / "*.gguf"), recursive=True)
                + glob.glob(str(models_dir / "*.gguf"))
            )
        )
        if not files:
            parent.add(rumps.MenuItem("No .gguf files found", callback=None))
            parent.add(rumps.MenuItem("Open Models Folder", callback=self.open_models_folder))
            return parent

        groups: dict[str, list[str]] = {}
        for path in files:
            rel = os.path.relpath(path, models_dir)
            parts = rel.split(os.sep)
            group = parts[0] if len(parts) > 1 else "__root__"
            groups.setdefault(group, []).append(path)

        for group, paths in sorted(groups.items(), key=lambda kv: kv[0].lower()):
            if group == "__root__":
                for path in paths:
                    parent.add(self._model_item(path, callback))
            else:
                sub = rumps.MenuItem(group)
                for path in sorted(paths, key=lambda p: os.path.basename(p).lower()):
                    sub.add(self._model_item(path, callback))
                parent.add(sub)

        return parent

    def _model_item(self, path: str, callback: Callable) -> rumps.MenuItem:
        p = optimal_params(path, self.cfg, self.ram_gb)
        size = model_size_gb(path)
        active = (
            self.rt.state in (ServerState.RUNNING, ServerState.STARTING)
            and self.rt.model_path
            and os.path.samefile(path, self.rt.model_path)
            if self.rt.model_path and os.path.exists(path) and os.path.exists(self.rt.model_path)
            else False
        )
        mark = "● " if active else ""
        label = f"{mark}{short_name(path)}   {fmt_size(size)} · ctx {fmt_ctx(p['ctx'])}"
        item = rumps.MenuItem(label)
        item.path = path
        item.set_callback(callback)
        return item

    # -- Actions -------------------------------------------------------------

    def start_server(self, sender):
        path = getattr(sender, "path", None)
        if not path:
            rumps.notification(APP_NAME, "No model selected", "")
            return

        binary = self.cfg.llama_server or discover_llama_server()
        if not binary or not os.access(binary, os.X_OK):
            self.rt.state = ServerState.ERROR
            self.rt.error_message = "llama-server binary not found"
            self.rebuild_menu()
            rumps.notification(
                APP_NAME,
                "llama-server not found",
                "Install with: brew install llama.cpp",
            )
            return

        if not os.path.isfile(path):
            self.rt.state = ServerState.ERROR
            self.rt.error_message = f"Model missing: {short_name(path)}"
            self.rebuild_menu()
            return

        with self._lock:
            if self.process_alive():
                self._kill_existing_server()

            p = optimal_params(path, self.cfg, self.ram_gb)
            self.rt.model_path = path
            self.rt.params = p
            self.rt.error_message = ""
            self.rt.state = ServerState.STARTING
            self.rt.started_at = time.time()

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_out = subprocess.DEVNULL
            try:
                self._close_log()
                self._log_fp = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
                self._log_fp.write(
                    f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start {short_name(path)} ---\n"
                )
                log_out = self._log_fp
            except Exception:
                self._log_fp = None
                log_out = subprocess.DEVNULL

            cmd = [
                binary,
                "--model",
                path,
                "--host",
                self.cfg.host,
                "--port",
                str(self.cfg.port),
                "-ngl",
                str(p["ngl"]),
                "-c",
                str(p["ctx"]),
                "-t",
                str(p["threads"]),
                "-b",
                str(p["batch"]),
            ]

            try:
                self.rt.proc = subprocess.Popen(
                    cmd,
                    stdout=log_out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception as exc:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = str(exc)
                self.rt.proc = None
                self._close_log()
                self.rebuild_menu()
                rumps.notification(APP_NAME, "Failed to start", str(exc))
                return

        self.rebuild_menu()
        rumps.notification(
            APP_NAME,
            f"Starting {short_name(path)}",
            f"ctx {fmt_ctx(p['ctx'])} · Metal ngl {p['ngl']}",
        )
        self._spawn_ready_watcher()

    def _spawn_ready_watcher(self) -> None:
        def watch():
            deadline = time.time() + READY_TIMEOUT_S
            while time.time() < deadline:
                with self._lock:
                    if self.rt.state != ServerState.STARTING:
                        return
                    if self.rt.proc is not None and self.rt.proc.poll() is not None:
                        code = self.rt.proc.returncode
                        self.rt.state = ServerState.ERROR
                        self.rt.error_message = f"Server exited (code {code})"
                        self.rt.proc = None
                        self._close_log()
                        break
                    if self.health_ok():
                        self.rt.state = ServerState.RUNNING
                        model = short_name(self.rt.model_path or "")
                        rumps.notification(APP_NAME, "Ready", f"{model} · {self.api_url}")
                        break
                time.sleep(READY_POLL_INTERVAL_S)
            else:
                with self._lock:
                    if self.rt.state == ServerState.STARTING:
                        self.rt.state = ServerState.ERROR
                        self.rt.error_message = "Timed out waiting for readiness"
                        self._kill_existing_server()
                        rumps.notification(
                            APP_NAME,
                            "Start timed out",
                            "Check Tools → Open Server Log",
                        )
            self.rebuild_menu()

        t = threading.Thread(target=watch, name="llama-ready", daemon=True)
        self._ready_thread = t
        t.start()

    def stop_server(self, _):
        with self._lock:
            self._kill_existing_server()
            self.rt.state = ServerState.STOPPED
            self.rt.model_path = None
            self.rt.params = {}
            self.rt.error_message = ""
        self.rebuild_menu()
        rumps.notification(APP_NAME, "Server stopped", "")

    def copy_url(self, _):
        copy_to_clipboard(self.api_url)
        rumps.notification(APP_NAME, "Copied API URL", self.api_url)

    def open_webui(self, _):
        open_url(self.webui_url)

    def open_models_folder(self, _):
        path = Path(self.cfg.models_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        open_path(path)

    def open_config_folder(self, _):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        open_path(CONFIG_DIR)

    def open_log(self, _):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not LOG_PATH.exists():
            LOG_PATH.write_text("", encoding="utf-8")
        open_path(LOG_PATH)

    def edit_config(self, _):
        self.cfg.save()
        open_path(CONFIG_PATH)

    def reload_config(self, _):
        with self._lock:
            self.cfg = AppConfig.load()
            self.cfg.ensure_dirs()
        self.rebuild_menu()
        rumps.notification(APP_NAME, "Config reloaded", str(CONFIG_PATH))

    def refresh_models(self, _):
        self.rebuild_menu()
        rumps.notification(APP_NAME, "Model list refreshed", self.cfg.models_dir)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Ensure clean exit doesn't leave zombies when Terminal is killed
    signal.signal(signal.SIGTERM, lambda *_: rumps.quit_application(None))
    LlamaCppApp().run()


if __name__ == "__main__":
    main()
