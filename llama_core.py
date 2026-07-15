#!/usr/bin/env python3
"""
Llama Menu core — server control + menu model (UI-agnostic).

Used by native_app.py (pure AppKit status bar).
"""

from __future__ import annotations

import atexit
import fcntl
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from launch_panel import LaunchPanel
except ImportError:
    LaunchPanel = None  # type: ignore

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_NAME = "Llama Menu"
BUNDLE_ID = "com.llamamenu.app"
# 8080 is often taken by Docker Desktop — use a dedicated port
DEFAULT_PORT = 8180
DEFAULT_HOST = "127.0.0.1"
DEFAULT_MODELS_DIR = Path.home() / "models"
CONFIG_DIR = Path.home() / ".config" / "llama-menu"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_DIR = CONFIG_DIR / "logs"
LOG_PATH = LOG_DIR / "server.log"
PID_LOCK_PATH = CONFIG_DIR / "llama-menu.lock"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"

HEALTH_TIMEOUT_S = 0.6
READY_POLL_INTERVAL_S = 0.5
READY_TIMEOUT_S = 120.0
OS_RESERVE_GB = 2.5

COPY = {
    "start": "Start Model",
    "switch": "Switch Model",
    "stop": "Stop",
    "open_chat": "Open Chat",
    "log": "Show Log",
    "quit": "Quit",
    "no_models": "No models yet",
    "add_models": "Open ~/models…",
    "missing_bin": "Install llama.cpp first",
    "on": "On",
    "off": "Off",
    "error": "Something went wrong",
}


def app_version() -> str:
    for p in (
        resource_path("VERSION"),
        Path(__file__).resolve().parent / "VERSION",
    ):
        try:
            if p and Path(p).is_file():
                return Path(p).read_text(encoding="utf-8").strip() or "0.0.0"
        except Exception:
            continue
    return "1.5.0"


def resource_path(*parts: str) -> Path:
    me = Path(__file__).resolve()
    bases = [
        me.parent,
        me.parent / "resources",
        me.parent.parent / "Resources",
    ]
    env = os.environ.get("LLAMA_MENU_RESOURCES")
    if env:
        bases.insert(0, Path(env))
    for base in bases:
        candidate = base.joinpath(*parts) if parts else base
        if candidate.exists():
            return candidate
    return bases[0].joinpath(*parts) if parts else bases[0]


def total_ram_gb() -> float:
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
        return int(out) / (1024**3)
    except Exception:
        return 16.0


def perf_core_count() -> int:
    for key in ("hw.perflevel0.logicalcpu", "hw.logicalcpu", "hw.ncpu"):
        try:
            n = int(subprocess.check_output(["sysctl", "-n", key], text=True).strip())
            if n > 0:
                return n
        except Exception:
            continue
    return max(1, os.cpu_count() or 4)


def discover_llama_server() -> Optional[str]:
    which = shutil.which("llama-server")
    if which and os.access(which, os.X_OK):
        return which
    for path in (
        "/opt/homebrew/bin/llama-server",
        "/usr/local/bin/llama-server",
        str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-server"),
    ):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def open_path(path: str | Path) -> None:
    subprocess.run(["/usr/bin/open", str(path)], check=False)


def open_url(url: str) -> None:
    for app in ("Safari", "Google Chrome", "Arc", "Microsoft Edge", "Firefox", "Brave Browser"):
        r = subprocess.run(["/usr/bin/open", "-a", app, url], capture_output=True)
        if r.returncode == 0:
            return
    subprocess.run(["/usr/bin/open", url], check=False)


def client_host(bind_host: str) -> str:
    h = (bind_host or DEFAULT_HOST).strip()
    if h in ("0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return h


def paths_equal(a: str, b: str) -> bool:
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normpath(os.path.expanduser(a)) == os.path.normpath(os.path.expanduser(b))


def model_size_gb(path: str) -> float:
    try:
        return os.path.getsize(path) / 1_073_741_824
    except OSError:
        return 0.0


def short_name(path: str, limit: int = 42) -> str:
    name = os.path.basename(path or "").replace(".gguf", "")
    if not name:
        return "unknown"
    return name[: limit - 1] + "…" if len(name) > limit else name


def fmt_size(gb: float) -> str:
    if gb >= 10:
        return f"{gb:.0f} GB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{gb * 1024:.0f} MB"


def find_mmproj(model_path: str) -> Optional[str]:
    """Find a multimodal projector next to a GGUF model (vision / audio).

    Looks in the model directory for common names:
      mmproj*.gguf, *mmproj*.gguf, projector*.gguf
    Prefers F16/F32 over quantized when multiple exist.
    """
    try:
        model_path = os.path.abspath(model_path)
        folder = Path(model_path).parent
        if not folder.is_dir():
            return None
        candidates: list[Path] = []
        for pat in ("mmproj*.gguf", "*mmproj*.gguf", "projector*.gguf", "*projector*.gguf"):
            candidates.extend(folder.glob(pat))
        # Unique, exclude the main model itself
        seen = set()
        files: list[Path] = []
        for c in candidates:
            key = str(c.resolve())
            if key in seen:
                continue
            if os.path.samefile(c, model_path) if c.exists() else False:
                continue
            seen.add(key)
            files.append(c)
        if not files:
            return None

        def rank(p: Path) -> tuple:
            name = p.name.lower()
            # Prefer full-precision projectors
            prec = 0 if any(x in name for x in ("f32", "f16", "fp16", "fp32")) else 1
            return (prec, len(name), name)

        files.sort(key=rank)
        return str(files[0].resolve())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------

class SingleInstance:
    def __init__(self, path: Path):
        self.path = path
        self._fp = None

    def acquire(self) -> bool:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._fp = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fp.close()
            self._fp = None
            return False
        self._fp.seek(0)
        self._fp.truncate()
        self._fp.write(str(os.getpid()))
        self._fp.flush()
        return True

    def release(self) -> None:
        if self._fp is not None:
            try:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
                self._fp.close()
            except Exception:
                pass
            self._fp = None


# ---------------------------------------------------------------------------
# Launch at login
# ---------------------------------------------------------------------------

def launch_at_login_program_args() -> list[str]:
    apps = Path("/Applications/Llama Menu.app")
    if apps.exists():
        return ["/usr/bin/open", "-a", str(apps)]
    return [sys.executable, str(Path(__file__).resolve().parent / "native_app.py")]


def set_launch_at_login(enabled: bool) -> bool:
    """Install LaunchAgent that only runs `open -a 'Llama Menu'` (no KeepAlive)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Never fight the old Platypus/KeepAlive agent
        legacy = Path.home() / "Library" / "LaunchAgents" / "com.local.llamacpp-menu.plist"
        if legacy.is_file():
            subprocess.run(["launchctl", "unload", str(legacy)], capture_output=True)
        if enabled:
            plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{BUNDLE_ID}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-a</string>
    <string>Llama Menu</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
"""
            LAUNCH_AGENT_PATH.write_text(plist, encoding="utf-8")
            subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT_PATH)], capture_output=True)
            subprocess.run(["launchctl", "load", str(LAUNCH_AGENT_PATH)], capture_output=True)
            return True
        if LAUNCH_AGENT_PATH.is_file():
            subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT_PATH)], capture_output=True)
            LAUNCH_AGENT_PATH.unlink(missing_ok=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    llama_server: str = ""
    models_dir: str = str(DEFAULT_MODELS_DIR)
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    ngl: int = 999
    batch: int = 512
    threads: int = 0
    auto_ctx: bool = True
    fixed_ctx: int = 8192
    launch_at_login: bool = True
    stop_server_on_quit: bool = True  # clean shutdown by default
    has_seen_welcome: bool = False
    has_set_default_launch_at_login: bool = False

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
        try:
            cfg.port = int(cfg.port)
            cfg.ngl = int(cfg.ngl)
            cfg.batch = int(cfg.batch)
            cfg.threads = int(cfg.threads)
            cfg.fixed_ctx = int(cfg.fixed_ctx)
        except (TypeError, ValueError):
            pass
        if not cfg.llama_server:
            cfg.llama_server = discover_llama_server() or ""
        if not cfg.threads:
            cfg.threads = perf_core_count()
        cfg.models_dir = str(Path(cfg.models_dir).expanduser())
        if not (1024 <= cfg.port <= 65535):
            cfg.port = DEFAULT_PORT
        # Migrate away from 8080 if Docker (or anything) owns it
        if cfg.port == 8080 and _port_busy(8080):
            cfg.port = DEFAULT_PORT
            try:
                cfg.save()
            except Exception:
                pass
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: getattr(self, k) for k in self.__dict__}
        CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def ensure_dirs(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)


def _port_busy(port: int) -> bool:
    """True if something other than our future bind might steal browser traffic."""
    try:
        r = subprocess.run(
            ["lsof", "-i", f"TCP:{port}", "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True,
            text=True,
        )
        out = r.stdout.lower()
        # Docker on *:8080 + llama on 127.0.0.1:8080 confuses browsers (IPv6)
        if "docker" in out or "com.docke" in out:
            return True
        # Multiple listeners = conflict risk
        lines = [ln for ln in r.stdout.splitlines() if "LISTEN" in ln]
        return len(lines) > 1
    except Exception:
        return False


def optimal_params(path: str, cfg: AppConfig, ram_gb: float) -> dict:
    try:
        from launch_panel import recommend_params

        rec = recommend_params(
            path, ram_gb, int(cfg.threads) or perf_core_count(), int(cfg.ngl), int(cfg.batch)
        )
        if not cfg.auto_ctx:
            rec["ctx"] = max(512, int(cfg.fixed_ctx))
        return {k: v for k, v in rec.items() if not str(k).startswith("_")}
    except Exception:
        size = model_size_gb(path)
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
        return {
            "ngl": int(cfg.ngl),
            "ctx": ctx if cfg.auto_ctx else max(512, int(cfg.fixed_ctx)),
            "threads": int(cfg.threads) or perf_core_count(),
            "batch": int(cfg.batch),
            "n_predict": -1,
            "seed": -1,
            "temperature": 0.7,
            "top_p": 0.95,
            "top_k": 40,
            "min_p": 0.05,
            "repeat_penalty": 1.1,
            "repeat_last_n": 64,
        }


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
# Controller
# ---------------------------------------------------------------------------

class LlamaController:
    """Business logic + builds NSMenu via ui callbacks."""

    def __init__(self, ui: Any):
        self.ui = ui  # AppDelegate — rebuild_menu uses ui.status_item + menu_target
        first_run = not CONFIG_PATH.is_file()
        self.cfg = AppConfig.load()
        self.cfg.ensure_dirs()
        if first_run:
            self.cfg.save()

        self.version = app_version()
        self.ram_gb = total_ram_gb()
        self._lock = threading.RLock()
        self.rt = Runtime()
        self._log_fp = None
        self._ready_gen = 0
        self._launch_panel = None
        self.status_item = None
        self._menu_sig: Optional[tuple] = None

        if not self.cfg.has_set_default_launch_at_login:
            self.cfg.has_set_default_launch_at_login = True
            if self.cfg.launch_at_login:
                set_launch_at_login(True)
            self.cfg.save()

        # If an orphan llama-server is already up on our port, adopt it
        alive, healthy = self._probe()
        with self._lock:
            self._apply_state(alive, healthy)
        # One-time cleanup: legacy server left on 8080 while we use 8180
        if self.cfg.port != 8080:
            try:
                subprocess.run(
                    ["pkill", "-f", "llama-server.*--port 8080"],
                    capture_output=True,
                )
            except Exception:
                pass

    def bind_status_item(self, item) -> None:
        self.status_item = item

    # -- URLs --
    @property
    def link_host(self) -> str:
        return client_host(self.cfg.host)

    @property
    def base_url(self) -> str:
        return f"http://{self.link_host}:{self.cfg.port}"

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/v1"

    @property
    def webui_url(self) -> str:
        return f"{self.base_url}/"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"

    def notify(self, title: str, message: str = "") -> None:
        try:
            from Foundation import NSUserNotification, NSUserNotificationCenter

            n = NSUserNotification.alloc().init()
            n.setTitle_(title)
            n.setInformativeText_(message)
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(n)
        except Exception:
            pass

    def maybe_welcome(self) -> None:
        if self.cfg.has_seen_welcome:
            return
        self.cfg.has_seen_welcome = True
        self.cfg.save()
        ok = bool(self.cfg.llama_server and os.access(self.cfg.llama_server, os.X_OK))
        self.notify(
            APP_NAME,
            "Click Llama → Start Model" if ok else "Install: brew install llama.cpp",
        )

    # -- Process --
    def _pgrep_server(self) -> bool:
        r = subprocess.run(
            ["pgrep", "-a", "-f", f"llama-server.*--port {self.cfg.port}"],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0 and bool(r.stdout.strip())

    def _port_has_llama(self) -> bool:
        r = subprocess.run(
            ["lsof", "-i", f"TCP:{self.cfg.port}", "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True,
            text=True,
        )
        return "llama" in r.stdout.lower()

    def process_alive(self) -> bool:
        proc = self.rt.proc
        if proc is not None:
            if proc.poll() is None:
                return True
            self.rt.proc = None
        return self._pgrep_server() or self._port_has_llama()

    def health_ok(self) -> bool:
        for url in (self.health_url, f"{self.api_url}/models"):
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_S) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except Exception:
                continue
        return False

    def _find_running_model(self) -> Optional[str]:
        r = subprocess.run(
            ["pgrep", "-a", "-f", "llama-server"],
            capture_output=True,
            text=True,
        )
        lines = [ln for ln in r.stdout.splitlines() if "--model" in ln]
        if not lines:
            return None
        port_token = f"--port {self.cfg.port}"
        preferred = [ln for ln in lines if port_token in ln or f"--port={self.cfg.port}" in ln]
        for line in preferred + lines:
            parts = line.split("--model", 1)
            arg = parts[1].strip()
            if arg.startswith("="):
                arg = arg[1:].strip()
            cand = arg.split()[0].strip().strip("\"'")
            if cand:
                return cand
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
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            except Exception:
                pass
        # Kill by port pattern (our config port)
        self._kill_port_listeners(self.cfg.port)
        subprocess.run(
            ["pkill", "-f", f"llama-server.*--port {self.cfg.port}"],
            capture_output=True,
        )
        # Also clear legacy 8080 if we used to run there (Docker may remain)
        if self.cfg.port != 8080:
            subprocess.run(
                ["pkill", "-f", "llama-server.*--port 8080"],
                capture_output=True,
            )
        for _ in range(30):
            if not self._pgrep_server() and not self._port_has_llama():
                break
            time.sleep(0.1)
        self._close_log()

    @staticmethod
    def _kill_port_listeners(port: int) -> None:
        """SIGTERM any process listening on port (llama only — not docker)."""
        try:
            r = subprocess.run(
                ["lsof", "-i", f"TCP:{port}", "-sTCP:LISTEN", "-n", "-P", "-t"],
                capture_output=True,
                text=True,
            )
            for pid_s in r.stdout.split():
                try:
                    pid = int(pid_s)
                    # Only kill llama-server, never docker
                    cmd = subprocess.check_output(
                        ["ps", "-p", str(pid), "-o", "args="], text=True
                    )
                    if "llama-server" in cmd or "llama-cli" in cmd:
                        os.kill(pid, signal.SIGTERM)
                except Exception:
                    continue
        except Exception:
            pass

    def _apply_state(self, alive: bool, healthy: bool) -> bool:
        changed = False
        if self.rt.state == ServerState.STARTING:
            if not alive:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = "Server exited before ready"
                self.rt.model_path = None
                self.rt.params = {}
                self.rt.proc = None
                self._close_log()
                changed = True
            elif healthy:
                self.rt.state = ServerState.RUNNING
                if not self.rt.model_path:
                    self.rt.model_path = self._find_running_model()
                changed = True
            elif time.time() - self.rt.started_at > READY_TIMEOUT_S:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = "Timed out waiting for readiness"
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
                if self.rt.model_path and not self.rt.params:
                    try:
                        self.rt.params = optimal_params(
                            self.rt.model_path, self.cfg, self.ram_gb
                        )
                    except Exception:
                        self.rt.params = {}
                changed = True
        return changed

    def _probe(self) -> tuple:
        with self._lock:
            alive = self.process_alive()
            state = self.rt.state
            need_health = alive and state in (
                ServerState.STARTING,
                ServerState.STOPPED,
                ServerState.ERROR,
            )
        if need_health:
            healthy = self.health_ok()
        else:
            healthy = alive and state == ServerState.RUNNING
        return alive, healthy

    def tick(self) -> None:
        alive, healthy = self._probe()
        with self._lock:
            changed = self._apply_state(alive, healthy)
            sig = self._compute_menu_sig()
            need_menu = changed or sig != self._menu_sig
        self.apply_appearance()
        if need_menu:
            self.rebuild_menu()

    def _compute_menu_sig(self) -> tuple:
        return (
            self.rt.state.value,
            self.rt.model_path or "",
            self.rt.error_message or "",
            self.cfg.port,
            self.cfg.models_dir,
        )

    # -- Appearance --
    def apply_appearance(self) -> None:
        item = self.status_item
        if item is None:
            return
        state = self.rt.state
        on = state == ServerState.RUNNING
        starting = state == ServerState.STARTING
        error = state == ServerState.ERROR

        # Keep emoji + word so the item is always obvious in a crowded menu bar
        if on:
            label = "🦙 Llama !"
        elif starting:
            label = "🦙 Llama …"
        elif error:
            label = "🦙 Llama ×"
        else:
            label = "🦙 Llama"

        # Icon path
        if on:
            names = ("menu_icon_on.png",)
            template = False
        elif starting:
            names = ("menu_icon_busy.png", "menu_icon_on.png")
            template = False
        elif error:
            names = ("menu_icon_error.png",)
            template = False
        else:
            names = ("menu_icon.png",)
            template = True

        img = None
        for name in names:
            p = resource_path(name)
            if Path(p).is_file():
                try:
                    from AppKit import NSImage
                    from Foundation import NSMakeSize

                    img = NSImage.alloc().initWithContentsOfFile_(str(p))
                    if img is not None and img.isValid():
                        img.setTemplate_(template)
                        img.setSize_(NSMakeSize(18.0, 18.0))
                        break
                except Exception:
                    img = None

        if img is None:
            try:
                from AppKit import NSImage
                from Foundation import NSMakeSize

                img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "brain", "Llama"
                )
                if img is not None:
                    img.setTemplate_(True)
                    img.setSize_(NSMakeSize(18.0, 18.0))
            except Exception:
                pass

        try:
            btn = item.button()
            if btn is not None:
                btn.setTitle_(label)
                if img is not None:
                    btn.setImage_(img)
                    if btn.image() is not None:
                        btn.image().setTemplate_(template)
                    try:
                        from AppKit import NSImageLeft
                        btn.setImagePosition_(NSImageLeft)
                    except Exception:
                        btn.setImagePosition_(2)
                btn.setAppearsDisabled_(False)
                btn.setAlphaValue_(1.0)
                btn.setToolTip_("On" if on else ("Starting…" if starting else ("Error" if error else "Off")))
            else:
                item.setTitle_(label)
                if img is not None:
                    item.setImage_(img)
        except Exception:
            pass

    # -- Menu building (NSMenu) --
    def rebuild_menu(self) -> None:
        from AppKit import NSMenu, NSMenuItem

        ui = self.ui
        target = ui.menu_target
        item = self.status_item
        if item is None:
            return

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        state = self.rt.state

        def add_disabled(title: str) -> None:
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            mi.setEnabled_(False)
            menu.addItem_(mi)

        def add_action(title: str, selector: str, represented=None) -> None:
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, selector, ""
            )
            mi.setTarget_(target)
            mi.setEnabled_(True)
            if represented is not None:
                mi.setRepresentedObject_(represented)
            menu.addItem_(mi)

        def add_sep() -> None:
            menu.addItem_(NSMenuItem.separatorItem())

        if state == ServerState.RUNNING:
            model = short_name(self.rt.model_path or "model", 28)
            vision = " · vision" if self.rt.params.get("mmproj") else ""
            add_disabled(f"●  On  ·  {model}{vision}")
            add_sep()
            add_action(COPY["open_chat"], "openChat:")
            add_action(COPY["stop"], "stopServer:")
            add_sep()
            self._add_model_submenu(menu, target, COPY["switch"])
            add_sep()
            self._add_stop_on_quit_item(menu, target)
            add_action(COPY["quit"], "quitApp:")
        elif state == ServerState.STARTING:
            model = short_name(self.rt.model_path or "model", 28)
            add_disabled(f"…  Starting  ·  {model}")
            add_sep()
            add_action(COPY["stop"], "stopServer:")
            add_sep()
            add_action(COPY["quit"], "quitApp:")
        elif state == ServerState.ERROR:
            err = self.rt.error_message or COPY["error"]
            if len(err) > 42:
                err = err[:39] + "…"
            add_disabled(f"!  {err}")
            add_sep()
            self._add_model_submenu(menu, target, COPY["start"])
            add_action(COPY["log"], "openLog:")
            add_sep()
            add_action(COPY["quit"], "quitApp:")
        else:
            add_disabled(f"○  {COPY['off']}")
            binary_ok = bool(
                self.cfg.llama_server and os.access(self.cfg.llama_server, os.X_OK)
            )
            if not binary_ok:
                add_disabled(COPY["missing_bin"])
            add_sep()
            self._add_model_submenu(menu, target, COPY["start"])
            add_sep()
            self._add_stop_on_quit_item(menu, target)
            add_action(COPY["quit"], "quitApp:")

        item.setMenu_(menu)
        self._menu_sig = self._compute_menu_sig()
        self.apply_appearance()

    def _add_stop_on_quit_item(self, menu, target) -> None:
        from AppKit import NSMenuItem, NSControlStateValueOn, NSControlStateValueOff

        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Stop Server on Quit", "toggleStopOnQuit:", ""
        )
        mi.setTarget_(target)
        mi.setEnabled_(True)
        mi.setState_(
            NSControlStateValueOn if self.cfg.stop_server_on_quit else NSControlStateValueOff
        )
        menu.addItem_(mi)

    def toggle_stop_on_quit(self) -> None:
        self.cfg.stop_server_on_quit = not self.cfg.stop_server_on_quit
        self.cfg.save()
        self.rebuild_menu()
        self.notify(
            APP_NAME,
            "Stop on quit: " + ("on" if self.cfg.stop_server_on_quit else "off"),
        )

    def _add_model_submenu(self, parent_menu, target, label: str) -> None:
        from AppKit import NSMenu, NSMenuItem

        sub_root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, None, "")
        sub_menu = NSMenu.alloc().init()
        sub_menu.setAutoenablesItems_(False)

        models_dir = Path(self.cfg.models_dir).expanduser()
        if not models_dir.is_dir():
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                COPY["no_models"], None, ""
            )
            mi.setEnabled_(False)
            sub_menu.addItem_(mi)
            sub_root.setSubmenu_(sub_menu)
            parent_menu.addItem_(sub_root)
            return

        files = sorted(set(glob.glob(str(models_dir / "**" / "*.gguf"), recursive=True)))
        if not files:
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                COPY["no_models"], None, ""
            )
            mi.setEnabled_(False)
            sub_menu.addItem_(mi)
            sub_root.setSubmenu_(sub_menu)
            parent_menu.addItem_(sub_root)
            return

        groups: dict[str, list[str]] = {}
        for path in files:
            rel = os.path.relpath(path, models_dir)
            parts = rel.split(os.sep)
            group = parts[0] if len(parts) > 1 else "__root__"
            groups.setdefault(group, []).append(path)

        for group, paths in sorted(groups.items(), key=lambda kv: kv[0].lower()):
            paths = sorted(paths, key=lambda p: os.path.basename(p).lower())
            if group == "__root__":
                for path in paths:
                    self._add_model_item(sub_menu, target, path)
            else:
                g_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    group, None, ""
                )
                g_menu = NSMenu.alloc().init()
                g_menu.setAutoenablesItems_(False)
                for path in paths:
                    self._add_model_item(g_menu, target, path)
                g_item.setSubmenu_(g_menu)
                sub_menu.addItem_(g_item)

        sub_root.setSubmenu_(sub_menu)
        parent_menu.addItem_(sub_root)

    def _add_model_item(self, menu, target, path: str) -> None:
        from AppKit import NSMenuItem

        size = model_size_gb(path)
        active = (
            self.rt.state in (ServerState.RUNNING, ServerState.STARTING)
            and bool(self.rt.model_path)
            and paths_equal(path, self.rt.model_path or "")
        )
        name = short_name(path, 30)
        prefix = "✓  " if active else "    "
        vision = " · 👁" if find_mmproj(path) else ""
        title = f"{prefix}{name}{vision}    {fmt_size(size)}"
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, "openLaunchForModel:", ""
        )
        mi.setTarget_(target)
        mi.setRepresentedObject_(path)
        mi.setEnabled_(True)
        menu.addItem_(mi)

    # -- Actions --
    def open_launch_panel(self, path: str) -> None:
        binary = self.cfg.llama_server or discover_llama_server()
        if not binary or not os.access(binary, os.X_OK):
            with self._lock:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = "llama-server binary not found"
            self.rebuild_menu()
            self.notify(APP_NAME, "Install: brew install llama.cpp")
            return
        if not os.path.isfile(path):
            with self._lock:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = f"Missing: {short_name(path)}"
            self.rebuild_menu()
            return

        if LaunchPanel is None:
            self._start_with_params(path, optimal_params(path, self.cfg, self.ram_gb))
            return

        try:
            if self._launch_panel is not None:
                self._launch_panel.close()
        except Exception:
            pass

        panel = LaunchPanel(
            model_path=path,
            ram_gb=self.ram_gb,
            threads=int(self.cfg.threads) or perf_core_count(),
            ngl=int(self.cfg.ngl),
            batch=int(self.cfg.batch),
            on_launch=self._on_panel_launch,
            on_cancel=lambda: None,
            open_url=open_url,
        )
        self._launch_panel = panel
        panel.open()
        self.notify(APP_NAME, f"Configure {short_name(path)}")

    def _on_panel_launch(self, path: str, params: dict) -> None:
        try:
            self._start_with_params(path, params)
        except Exception as exc:
            with self._lock:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = str(exc)
            self.rebuild_menu()
            self.notify(APP_NAME, str(exc))

    def _start_with_params(self, path: str, p: dict) -> None:
        binary = self.cfg.llama_server or discover_llama_server()
        if not binary:
            return
        # Avoid Docker / multi-listener mess on 8080
        if self.cfg.port == 8080 and _port_busy(8080):
            self.cfg.port = DEFAULT_PORT
            self.cfg.save()
            self.notify(APP_NAME, f"Port busy — using {DEFAULT_PORT}")
        with self._lock:
            self._ready_gen += 1
            ready_gen = self._ready_gen
            # Always free our port before start (orphan servers are common)
            self._kill_existing_server()

            self.rt.model_path = path
            self.rt.params = dict(p)
            self.rt.error_message = ""
            self.rt.state = ServerState.STARTING
            self.rt.started_at = time.time()

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_out = subprocess.DEVNULL
            try:
                self._close_log()
                self._log_fp = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
                self._log_fp.write(
                    f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {short_name(path)} ---\n"
                )
                self._log_fp.write(f"params: {json.dumps(p)}\n")
                log_out = self._log_fp
            except Exception:
                self._log_fp = None
                log_out = subprocess.DEVNULL

            # Cap ctx hard — huge contexts make WebUI show "Loading model" for minutes
            ctx = max(512, min(int(p.get("ctx", 8192)), 65536))
            parallel = max(1, min(int(p.get("parallel", 1)), 4))
            mmproj = p.get("mmproj") or find_mmproj(path)
            if mmproj:
                self.rt.params["mmproj"] = mmproj
            cmd = [
                binary,
                "--model", path,
                "--host", self.cfg.host,
                "--port", str(self.cfg.port),
                "-ngl", str(int(p.get("ngl", 999))),
                "-c", str(ctx),
                "-t", str(int(p.get("threads", 4))),
                "-b", str(int(p.get("batch", 512))),
                "-np", str(parallel),  # 1 slot = faster load, less RAM
                "-n", str(int(p.get("n_predict", -1))),
                "-s", str(int(p.get("seed", -1))),
                "--temp", str(float(p.get("temperature", 0.7))),
                "--top-p", str(float(p.get("top_p", 0.95))),
                "--top-k", str(int(p.get("top_k", 40))),
                "--min-p", str(float(p.get("min_p", 0.05))),
                "--repeat-penalty", str(float(p.get("repeat_penalty", 1.1))),
                "--repeat-last-n", str(int(p.get("repeat_last_n", 64))),
                "--jinja",  # chat templates for WebUI
            ]
            if mmproj and os.path.isfile(str(mmproj)):
                cmd.extend(["--mmproj", str(mmproj)])
            try:
                if self._log_fp:
                    self._log_fp.write("cmd: " + " ".join(cmd) + "\n")
                    if mmproj:
                        self._log_fp.write(f"mmproj: {mmproj}\n")
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
                self.notify(APP_NAME, str(exc))
                return

        self.rebuild_menu()
        mm = self.rt.params.get("mmproj")
        note = short_name(path)
        if mm:
            note = f"{note} + vision"
        self.notify(APP_NAME, f"Starting {note}")
        self._spawn_ready_watcher(ready_gen)

    def _spawn_ready_watcher(self, ready_gen: int) -> None:
        def watch():
            deadline = time.time() + READY_TIMEOUT_S
            while time.time() < deadline:
                with self._lock:
                    if ready_gen != self._ready_gen or self.rt.state != ServerState.STARTING:
                        return
                    if self.rt.proc is not None and self.rt.proc.poll() is not None:
                        code = self.rt.proc.returncode
                        self.rt.state = ServerState.ERROR
                        self.rt.error_message = f"Server exited (code {code})"
                        self.rt.proc = None
                        self._close_log()
                        break
                if self.health_ok():
                    with self._lock:
                        if ready_gen != self._ready_gen:
                            return
                        self.rt.state = ServerState.RUNNING
                    self.notify(APP_NAME, f"Ready · {short_name(self.rt.model_path or '')}")
                    # UI updates must be on main thread
                    try:
                        from PyObjCTools import AppHelper
                        AppHelper.callAfter(self.rebuild_menu)
                    except Exception:
                        self.rebuild_menu()
                    return
                time.sleep(READY_POLL_INTERVAL_S)
            else:
                with self._lock:
                    if ready_gen == self._ready_gen and self.rt.state == ServerState.STARTING:
                        self.rt.state = ServerState.ERROR
                        self.rt.error_message = "Timed out"
                        self._kill_existing_server()
            try:
                from PyObjCTools import AppHelper
                AppHelper.callAfter(self.rebuild_menu)
            except Exception:
                self.rebuild_menu()

        threading.Thread(target=watch, name="llama-ready", daemon=True).start()

    def stop_server(self) -> None:
        with self._lock:
            self._ready_gen += 1
            self._kill_existing_server()
            self.rt.state = ServerState.STOPPED
            self.rt.model_path = None
            self.rt.params = {}
            self.rt.error_message = ""
        self.rebuild_menu()
        self.notify(APP_NAME, "Stopped")

    def open_webui(self) -> None:
        if self.rt.state != ServerState.RUNNING:
            self.notify(APP_NAME, "Start a model first — wait until it says Ready")
            return
        if not self.health_ok():
            self.notify(APP_NAME, "Still loading model — wait a few seconds")
            return
        # Prefer explicit IPv4 URL so we never hit Docker on ::1:8080
        url = f"http://127.0.0.1:{self.cfg.port}/"
        open_url(url)
        self.notify(APP_NAME, f"Chat · {url}")

    def open_log(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not LOG_PATH.exists():
            LOG_PATH.write_text("", encoding="utf-8")
        open_path(LOG_PATH)

    def quit_app(self) -> None:
        if self.cfg.stop_server_on_quit:
            with self._lock:
                self._ready_gen += 1
                self._kill_existing_server()
        from AppKit import NSApp
        NSApp.terminate_(None)

    def shutdown(self) -> None:
        if self.cfg.stop_server_on_quit:
            self._kill_existing_server()
