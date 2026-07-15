#!/usr/bin/env python3
"""
Llama Menu — shippable macOS menu bar controller for llama-server.

Product patterns borrowed from:
  - Llama-macOS (status states, launch-at-login default, accessory policy,
    health-aware server lifecycle, settings surface)
  - llama-server-osx (model folders, logs, config under ~/.config, simple
    start/stop/switch workflow)
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

import rumps

try:
    from AppKit import NSApplication
except Exception:  # pragma: no cover
    NSApplication = None

try:
    from launch_panel import LaunchPanel
except ImportError:
    # Bundled next to script in Resources/
    import importlib.util

    _lp = Path(__file__).resolve().parent / "launch_panel.py"
    if _lp.is_file():
        _spec = importlib.util.spec_from_file_location("launch_panel", _lp)
        _mod = importlib.util.module_from_spec(_spec)
        assert _spec.loader is not None
        _spec.loader.exec_module(_mod)
        LaunchPanel = _mod.LaunchPanel
    else:
        LaunchPanel = None  # type: ignore

# ---------------------------------------------------------------------------
# Identity & paths
# ---------------------------------------------------------------------------

APP_NAME = "Llama Menu"
BUNDLE_ID = "com.llamamenu.app"
LAUNCH_AGENT_LABEL = BUNDLE_ID
DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"

# Menu copy — only what people actually use
COPY = {
    "starting": "Starting…",
    "error": "Something went wrong",
    "start": "Start Model",
    "switch": "Switch Model",
    "stop": "Stop",
    "open_chat": "Open Chat",
    "log": "Show Log",
    "quit": "Quit",
    "no_models": "No models yet",
    "add_models": "Open ~/models…",
    "missing_bin": "Install llama.cpp to get started",
    "brew_hint": "brew install llama.cpp",
    "on": "On",
    "off": "Off",
}

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
TICK_INTERVAL_S = 2.0
OS_RESERVE_GB = 2.5


def app_version() -> str:
    """Read version from bundle Resources/VERSION, repo VERSION, or fallback."""
    candidates = [
        resource_path("VERSION"),
        Path(__file__).resolve().parent / "VERSION",
        Path(sys.executable).resolve().parent.parent / "Resources" / "VERSION",
    ]
    for p in candidates:
        try:
            if p and Path(p).is_file():
                return Path(p).read_text(encoding="utf-8").strip() or "0.0.0"
        except Exception:
            continue
    return "1.2.0"


def resource_path(*parts: str) -> Path:
    """Resolve a file next to the script or inside the .app Resources folder."""
    # When frozen / bundled: Contents/Resources/
    me = Path(__file__).resolve()
    bases = [
        me.parent,
        me.parent / "resources",
        me.parent.parent / "Resources",  # .../Contents/MacOS -> Contents/Resources
        Path(getattr(sys, "_MEIPASS", me.parent)),  # pyinstaller-style
    ]
    # Environment override for tests/dev
    env = os.environ.get("LLAMA_MENU_RESOURCES")
    if env:
        bases.insert(0, Path(env))

    for base in bases:
        candidate = base.joinpath(*parts)
        if candidate.exists():
            return candidate
    return bases[0].joinpath(*parts)


def running_from_app_bundle() -> bool:
    p = Path(sys.argv[0]).resolve()
    return ".app/Contents/" in str(p) or p.name == "Llama Menu"


def app_executable_path() -> str:
    """Path used for LaunchAgents — prefer .app open, else this script."""
    # If launched via .app MacOS launcher, argv[0] is the launcher
    argv0 = Path(sys.argv[0]).resolve()
    s = str(argv0)
    marker = ".app/Contents/"
    if marker in s:
        return s.split(marker)[0] + ".app"
    # Installed location
    apps = Path("/Applications/Llama Menu.app")
    if apps.exists():
        return str(apps)
    local = Path.home() / "Applications" / "Llama Menu.app"
    if local.exists():
        return str(local)
    # Dev fallback: this python + script
    return str(Path(__file__).resolve())


# ---------------------------------------------------------------------------
# System helpers
# ---------------------------------------------------------------------------

def _set_accessory_activation_policy() -> None:
    if NSApplication is None:
        return
    try:
        NSApplication.sharedApplication().setActivationPolicy_(1)
    except Exception:
        pass


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
        str(Path.home() / ".local" / "bin" / "llama-server"),
    ):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def open_path(path: str | Path) -> None:
    subprocess.run(["/usr/bin/open", str(path)], check=False)


def open_url(url: str) -> None:
    """Open URL in a real system browser (not an IDE embedded browser).

    Cursor/VS Code simple browsers often fail on llama-server's ES-module WebUI
    (blank page). Prefer Safari/Chrome, then the macOS default handler.
    """
    # Prefer well-known browsers that handle the WebUI SPA correctly
    for app in ("Safari", "Google Chrome", "Arc", "Microsoft Edge", "Firefox", "Brave Browser"):
        r = subprocess.run(
            ["/usr/bin/open", "-a", app, url],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return
    # Fallback: default handler
    subprocess.run(["/usr/bin/open", url], check=False)


def copy_to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode(), check=False)


def paths_equal(a: str, b: str) -> bool:
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normpath(os.path.expanduser(a)) == os.path.normpath(os.path.expanduser(b))


def client_host(bind_host: str) -> str:
    h = (bind_host or DEFAULT_HOST).strip()
    if h in ("0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return h


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

class SingleInstance:
    """Advisory lock so only one menu bar controller runs."""

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
# Launch at login (LaunchAgent — works for unsigned .app / script)
# ---------------------------------------------------------------------------

def launch_agent_plist(program_args: list[str]) -> str:
    args_xml = "\n".join(f"    <string>{_xml_escape(a)}</string>" for a in program_args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{BUNDLE_ID}</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardOutPath</key>
  <string>{_xml_escape(str(LOG_DIR / "launchd.out.log"))}</string>
  <key>StandardErrorPath</key>
  <string>{_xml_escape(str(LOG_DIR / "launchd.err.log"))}</string>
</dict>
</plist>
"""


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def launch_at_login_program_args() -> list[str]:
    """How launchd should start the app."""
    target = app_executable_path()
    if target.endswith(".app"):
        return ["/usr/bin/open", "-a", target]
    # Dev / script mode
    py = sys.executable
    # Prefer framework/CLT python that has rumps if available
    return [py, str(Path(__file__).resolve())]


def is_launch_at_login_enabled() -> bool:
    return LAUNCH_AGENT_PATH.is_file()


def set_launch_at_login(enabled: bool) -> bool:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        if enabled:
            plist = launch_agent_plist(launch_at_login_program_args())
            LAUNCH_AGENT_PATH.write_text(plist, encoding="utf-8")
            subprocess.run(
                ["launchctl", "unload", str(LAUNCH_AGENT_PATH)],
                capture_output=True,
            )
            r = subprocess.run(
                ["launchctl", "load", str(LAUNCH_AGENT_PATH)],
                capture_output=True,
                text=True,
            )
            return r.returncode == 0 or LAUNCH_AGENT_PATH.is_file()
        # disable
        if LAUNCH_AGENT_PATH.is_file():
            subprocess.run(
                ["launchctl", "unload", str(LAUNCH_AGENT_PATH)],
                capture_output=True,
            )
            LAUNCH_AGENT_PATH.unlink(missing_ok=True)
        return True
    except Exception:
        return False


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
    threads: int = 0
    auto_ctx: bool = True
    fixed_ctx: int = 8192
    launch_at_login: bool = True  # product default (Llama-macOS style)
    stop_server_on_quit: bool = False
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
            cfg.auto_ctx = bool(cfg.auto_ctx)
            cfg.launch_at_login = bool(cfg.launch_at_login)
            cfg.stop_server_on_quit = bool(cfg.stop_server_on_quit)
            cfg.has_seen_welcome = bool(cfg.has_seen_welcome)
            cfg.has_set_default_launch_at_login = bool(cfg.has_set_default_launch_at_login)
        except (TypeError, ValueError):
            pass
        if not cfg.llama_server:
            cfg.llama_server = discover_llama_server() or ""
        if not cfg.threads:
            cfg.threads = perf_core_count()
        cfg.models_dir = str(Path(cfg.models_dir).expanduser())
        # Clamp port like Llama-macOS (non-privileged)
        if not (1024 <= cfg.port <= 65535):
            cfg.port = DEFAULT_PORT
        # Migrate older configs missing product keys
        if CONFIG_PATH.is_file():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                missing = [k for k in cfg.__dict__ if k not in raw]
                if missing:
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


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------

def model_size_gb(path: str) -> float:
    try:
        return os.path.getsize(path) / 1_073_741_824
    except OSError:
        return 0.0


def optimal_params(path: str, cfg: AppConfig, ram_gb: float) -> dict:
    """Runtime defaults for this Mac (also used by the launch panel)."""
    try:
        from launch_panel import recommend_params

        rec = recommend_params(
            path,
            ram_gb,
            int(cfg.threads) or perf_core_count(),
            int(cfg.ngl),
            int(cfg.batch),
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
        if not cfg.auto_ctx:
            ctx = max(512, int(cfg.fixed_ctx))
        return {
            "ngl": int(cfg.ngl),
            "ctx": ctx,
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


def short_name(path: str, limit: int = 42) -> str:
    name = os.path.basename(path or "").replace(".gguf", "")
    if not name:
        return "unknown"
    return name[: limit - 1] + "…" if len(name) > limit else name


def fmt_ctx(ctx: int) -> str:
    return f"{ctx // 1024}K" if ctx >= 1024 else str(ctx)


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

        # ALWAYS start with a visible title. Empty title + failed image = invisible bar item.
        self._icon_path = self._resolve_menu_icon()
        super().__init__(
            name=APP_NAME,
            title="Llama",
            icon=None,  # apply ourselves after status item exists (more reliable)
            template=None,
            quit_button=None,
        )
        self._use_icon = bool(self._icon_path)
        self._last_icon_state: Optional[str] = None

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
        self._menu_signature: Optional[tuple] = None
        self._ready_gen = 0
        self._pending_notify: Optional[tuple] = None
        self._force_rebuild = False
        self._launch_panel: Optional[Any] = None
        # Pure AppKit status item (rumps item is often invisible on modern macOS)
        self._native_item = None
        self._status_ready = False

        # Launch-at-login: enable once by default (Llama-macOS pattern)
        if not self.cfg.has_set_default_launch_at_login:
            self.cfg.has_set_default_launch_at_login = True
            if self.cfg.launch_at_login:
                set_launch_at_login(True)
            self.cfg.save()

        alive, healthy = self._probe()
        with self._lock:
            self._apply_state(alive, healthy)
        self._do_rebuild(force=True)
        rumps.Timer(self.tick, TICK_INTERVAL_S).start()
        # Ensure status item icon is applied once the NSStatusItem exists
        rumps.Timer(self._ensure_status_icon, 0.3).start()

        if not self.cfg.has_seen_welcome:
            # Defer slightly so status item exists
            rumps.Timer(self._welcome_once, 1).start()

    @staticmethod
    def _resolve_menu_icon() -> Optional[str]:
        """Default OFF icon path."""
        return LlamaCppApp._icon_path_for_state(ServerState.STOPPED)

    @staticmethod
    def _icon_path_for_state(state: "ServerState") -> Optional[str]:
        """Pick glyph file for state (colored PNGs for on/busy/error)."""
        if state == ServerState.RUNNING:
            names = ("menu_icon_on.png", "menu_icon.png")
        elif state == ServerState.STARTING:
            names = ("menu_icon_busy.png", "menu_icon_on.png", "menu_icon.png")
        elif state == ServerState.ERROR:
            names = ("menu_icon_error.png", "menu_icon_busy.png", "menu_icon.png")
        else:
            names = ("menu_icon.png",)

        search_dirs = [
            resource_path(""),
            Path(__file__).resolve().parent,
            Path(__file__).resolve().parent / "resources",
        ]
        for name in names:
            for d in search_dirs:
                try:
                    candidate = Path(d) / name
                    if candidate.is_file() and candidate.stat().st_size > 0:
                        return str(candidate.resolve())
                except Exception:
                    continue
            try:
                p = resource_path(name)
                if Path(p).is_file():
                    return str(Path(p).resolve())
            except Exception:
                pass
        return None

    @staticmethod
    def _icon_is_template(state: "ServerState") -> bool:
        """Colored status icons must NOT be template (macOS would strip color)."""
        return state == ServerState.STOPPED

    def _log_ui(self, msg: str) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / "ui.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    def _load_nsimage(self, path: Optional[str], *, template: bool):
        """Simple NSImage load; fall back to SF Symbol."""
        try:
            from AppKit import NSImage
            from Foundation import NSMakeSize

            img = None
            if path and Path(path).is_file():
                p = Path(path)
                hi = p.with_name(p.stem + "@2x.png")
                img = NSImage.alloc().initWithContentsOfFile_(
                    str(hi if hi.is_file() else p)
                )

            if img is None or not img.isValid():
                for sym in ("brain", "sparkles", "circle.fill"):
                    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                        sym, "Llama Menu"
                    )
                    if img is not None:
                        break

            if img is None:
                return None
            img.setTemplate_(bool(template))
            img.setSize_(NSMakeSize(18.0, 18.0))
            return img
        except Exception as exc:
            self._log_ui(f"load image fail: {exc}")
            return None

    def _ensure_status_icon(self, timer=None):
        """Create a pure-AppKit status item that is always visible."""
        try:
            if timer is not None:
                timer.stop()
        except Exception:
            pass

        try:
            self._install_native_status_item()
            self._last_icon_state = None
            self._apply_status_appearance()
            self._status_ready = self._native_item is not None
            self._log_ui(f"status ready={self._status_ready}")
        except Exception as exc:
            self._log_ui(f"ensure status fail: {exc}")

        # Retry until we have a native item (rumps may not be up yet)
        if self._native_item is None:
            rumps.Timer(self._ensure_status_icon, 0.4).start()

    def _install_native_status_item(self) -> None:
        """Own NSStatusItem via AppKit — independent of rumps visibility bugs."""
        if self._native_item is not None:
            # Keep menu synced with rumps
            self._sync_native_menu()
            return

        try:
            from AppKit import NSStatusBar, NSVariableStatusItemLength, NSImageLeft
        except Exception as exc:
            self._log_ui(f"AppKit import fail: {exc}")
            return

        try:
            bar = NSStatusBar.systemStatusBar()
            item = bar.statusItemWithLength_(NSVariableStatusItemLength)
            btn = item.button()
            if btn is None:
                self._log_ui("status item has no button")
                return
            btn.setTitle_("Llama")
            btn.setToolTip_("Llama Menu")
            try:
                btn.setImagePosition_(NSImageLeft)
            except Exception:
                pass

            self._native_item = item
            self._sync_native_menu()

            # Hide rumps' own status item if present (avoids a blank slot)
            try:
                rumps_item = self._rumps_status_item()
                if rumps_item is not None:
                    rumps_item.setLength_(0)
            except Exception:
                pass

            self._log_ui("native status item installed")
        except Exception as exc:
            self._log_ui(f"install native fail: {exc}")

    def _rumps_status_item(self):
        nsapp = getattr(self, "_nsapp", None)
        if nsapp is None:
            return None
        return getattr(nsapp, "nsstatusitem", None)

    def _sync_native_menu(self) -> None:
        """Point our visible item at the rumps NSMenu."""
        if self._native_item is None:
            return
        try:
            rumps_item = self._rumps_status_item()
            menu = None
            if rumps_item is not None:
                menu = rumps_item.menu()
            if menu is None:
                # Fallback: rumps App menu object
                try:
                    menu = self._menu._menu  # type: ignore[attr-defined]
                except Exception:
                    menu = None
            if menu is not None:
                self._native_item.setMenu_(menu)
        except Exception as exc:
            self._log_ui(f"sync menu fail: {exc}")

    def _status_item(self):
        """Prefer native visible item; fall back to rumps."""
        if self._native_item is not None:
            return self._native_item
        return self._rumps_status_item()

    def _apply_status_appearance(self) -> None:
        """Drive the native status item: title + colored icon.

          Off:      monochrome tile + "Llama"
          On:       green tile + "Llama !"
          Starting: orange + "Llama …"
          Error:    red + "Llama ×"
        """
        self._install_native_status_item()
        item = self._status_item()
        state = self.rt.state
        self._last_icon_state = state.value

        on = state == ServerState.RUNNING
        starting = state == ServerState.STARTING
        error = state == ServerState.ERROR
        as_template = state == ServerState.STOPPED

        if on:
            label = "Llama !"
        elif starting:
            label = "Llama …"
        elif error:
            label = "Llama ×"
        else:
            label = "Llama"

        # Keep rumps in sync (even if hidden)
        try:
            self.title = label
        except Exception:
            pass

        path = self._icon_path_for_state(state)
        img = self._load_nsimage(path, template=as_template)

        if item is None:
            self._log_ui("no status item yet")
            return

        try:
            btn = item.button() if hasattr(item, "button") else None
            if btn is not None:
                btn.setTitle_(label)
                btn.setAppearsDisabled_(False)
                btn.setAlphaValue_(1.0)
                if img is not None:
                    btn.setImage_(img)
                    if btn.image() is not None:
                        btn.image().setTemplate_(bool(as_template))
                    try:
                        from AppKit import NSImageLeft
                        btn.setImagePosition_(NSImageLeft)
                    except Exception:
                        btn.setImagePosition_(2)
                else:
                    btn.setImage_(None)
                if on:
                    btn.setToolTip_(
                        f"On · {short_name(self.rt.model_path or 'model', 40)}"
                    )
                elif starting:
                    btn.setToolTip_("Starting…")
                elif error:
                    btn.setToolTip_("Error")
                else:
                    btn.setToolTip_("Off — click to start a model")
            else:
                item.setTitle_(label)
                if img is not None:
                    item.setImage_(img)
        except Exception as exc:
            self._log_ui(f"apply appearance fail: {exc}")

        self._sync_native_menu()
        self._use_icon = img is not None

    def _welcome_once(self, _timer=None):
        if self.cfg.has_seen_welcome:
            return
        self.cfg.has_seen_welcome = True
        self.cfg.save()
        binary_ok = bool(self.cfg.llama_server and os.access(self.cfg.llama_server, os.X_OK))
        msg = (
            "Click the icon → Start Model."
            if binary_ok
            else COPY["brew_hint"]
        )
        self._notify_now(APP_NAME, "Welcome", msg)
        try:
            _timer.stop()
        except Exception:
            pass

    # -- URLs ----------------------------------------------------------------

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

    # -- UI helpers ----------------------------------------------------------

    def _notify_now(self, title: str, subtitle: str = "", message: str = "") -> None:
        try:
            rumps.notification(title, subtitle, message)
        except Exception:
            pass

    def _request_rebuild(self) -> None:
        with self._lock:
            self._force_rebuild = True
            self._menu_signature = None

    def _set_title_for_state(self) -> None:
        """Keep menu-bar glyph in sync with server state (on/off/dim)."""
        self._apply_status_appearance()

    # -- Process / health ----------------------------------------------------

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
        port_eq = f"--port={self.cfg.port}"
        preferred = [ln for ln in lines if port_token in ln or port_eq in ln]
        ordered = preferred + [ln for ln in lines if ln not in preferred]
        for line in ordered:
            parts = line.split("--model", 1)
            model_arg = parts[1].strip()
            if not model_arg:
                continue
            if model_arg.startswith("="):
                model_arg = model_arg[1:].strip()
            candidate = model_arg.split()[0].strip().strip("\"'")
            if candidate:
                return candidate
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
        subprocess.run(
            ["pkill", "-f", f"llama-server.*--port {self.cfg.port}"],
            capture_output=True,
        )
        for _ in range(30):
            if not self._pgrep_server() and not self._port_has_llama():
                break
            time.sleep(0.1)
        self._close_log()

    def _apply_state(self, alive: bool, healthy: bool) -> bool:
        changed = False
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
                if not self.rt.model_path:
                    self.rt.model_path = self._find_running_model()
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

    def _menu_sig(self) -> tuple:
        p = self.rt.params or {}
        return (
            self.rt.state.value,
            self.rt.model_path or "",
            self.rt.error_message or "",
            p.get("ctx"),
            self.cfg.port,
            self.cfg.models_dir,
            self.cfg.llama_server,
        )

    def tick(self, _):
        alive, healthy = self._probe()
        notify = None
        with self._lock:
            changed = self._apply_state(alive, healthy)
            force = self._force_rebuild
            self._force_rebuild = False
            if self._pending_notify:
                notify = self._pending_notify
                self._pending_notify = None
            self._set_title_for_state()
            if force or changed or self._menu_signature != self._menu_sig():
                self._do_rebuild(force=True)
        if notify:
            self._notify_now(*notify)

    # -- Menu ----------------------------------------------------------------

    def rebuild_menu(self, force: bool = False):
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._do_rebuild(force=force)
        finally:
            self._lock.release()

    def _do_rebuild(self, force: bool = False):
        """Build a calm, action-first menu (progressive disclosure)."""
        sig = self._menu_sig()
        if not force and sig == self._menu_signature:
            self._set_title_for_state()
            return
        self._menu_signature = sig
        self._set_title_for_state()
        self.menu.clear()
        self.menu = self._build_menu_items()
        # Keep native status item menu pointer fresh after rebuilds
        try:
            self._sync_native_menu()
        except Exception:
            pass

    def _build_menu_items(self) -> list:
        """
        Only useful actions:

          Off:   Off · Start Model · Quit
          On:    On · model · Open Chat · Stop · Switch · Quit
        """
        state = self.rt.state
        items: list = []

        if state == ServerState.RUNNING:
            model = short_name(self.rt.model_path or "model", limit=28)
            items.append(rumps.MenuItem(f"●  On  ·  {model}", callback=None))
            items.append(None)
            items.append(rumps.MenuItem(COPY["open_chat"], callback=self.open_webui))
            items.append(rumps.MenuItem(COPY["stop"], callback=self.stop_server))
            items.append(None)
            items.append(self._model_submenu(COPY["switch"], self.start_server))
            items.append(None)
            items.append(rumps.MenuItem(COPY["quit"], callback=self.quit_app))
            return items

        if state == ServerState.STARTING:
            model = short_name(self.rt.model_path or "model", limit=28)
            items.append(rumps.MenuItem(f"…  Starting  ·  {model}", callback=None))
            items.append(None)
            items.append(rumps.MenuItem(COPY["stop"], callback=self.stop_server))
            items.append(None)
            items.append(rumps.MenuItem(COPY["quit"], callback=self.quit_app))
            return items

        if state == ServerState.ERROR:
            err = self.rt.error_message or COPY["error"]
            if len(err) > 42:
                err = err[:39] + "…"
            items.append(rumps.MenuItem(f"!  {err}", callback=None))
            items.append(None)
            items.append(self._model_submenu(COPY["start"], self.start_server))
            items.append(rumps.MenuItem(COPY["log"], callback=self.open_log))
            items.append(None)
            items.append(rumps.MenuItem(COPY["quit"], callback=self.quit_app))
            return items

        # Off
        items.append(rumps.MenuItem(f"○  {COPY['off']}", callback=None))
        binary_ok = bool(
            self.cfg.llama_server and os.access(self.cfg.llama_server, os.X_OK)
        )
        if not binary_ok:
            items.append(rumps.MenuItem(COPY["missing_bin"], callback=None))
        items.append(None)
        items.append(self._model_submenu(COPY["start"], self.start_server))
        items.append(None)
        items.append(rumps.MenuItem(COPY["quit"], callback=self.quit_app))
        return items

    def _model_submenu(self, label: str, callback: Callable) -> rumps.MenuItem:
        parent = rumps.MenuItem(label)
        models_dir = Path(self.cfg.models_dir).expanduser()

        if not models_dir.is_dir():
            parent.add(rumps.MenuItem(COPY["no_models"], callback=None))
            parent.add(rumps.MenuItem(COPY["add_models"], callback=self.open_models_folder))
            return parent

        files = sorted(set(glob.glob(str(models_dir / "**" / "*.gguf"), recursive=True)))
        if not files:
            parent.add(rumps.MenuItem(COPY["no_models"], callback=None))
            parent.add(rumps.MenuItem(COPY["add_models"], callback=self.open_models_folder))
            return parent

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
                    parent.add(self._model_item(path, callback))
            else:
                # Folder as friendly section name (no emoji clutter)
                sub = rumps.MenuItem(group)
                for path in paths:
                    sub.add(self._model_item(path, callback))
                parent.add(sub)
        return parent

    def _model_item(self, path: str, callback: Callable) -> rumps.MenuItem:
        """Clean model row: Name                    4.2 GB"""
        size = model_size_gb(path)
        active = (
            self.rt.state in (ServerState.RUNNING, ServerState.STARTING)
            and bool(self.rt.model_path)
            and paths_equal(path, self.rt.model_path or "")
        )
        name = short_name(path, limit=34)
        # Use a checkmark feel without noisy metadata; size on the right via spaces
        prefix = "✓  " if active else "    "
        label = f"{prefix}{name}    {fmt_size(size)}"
        item = rumps.MenuItem(label)
        item.path = path
        item.set_callback(callback)
        return item

    # -- Actions -------------------------------------------------------------

    def start_server(self, sender):
        """Open the launch panel for the selected model (settings → Start)."""
        path = getattr(sender, "path", None)
        if not path:
            self._notify_now(APP_NAME, "No model selected", "")
            return

        binary = self.cfg.llama_server or discover_llama_server()
        if not binary or not os.access(binary, os.X_OK):
            with self._lock:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = "llama-server binary not found"
            self.rebuild_menu(force=True)
            self._notify_now(APP_NAME, "llama-server not found", "brew install llama.cpp")
            return

        if not os.path.isfile(path):
            with self._lock:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = f"Model missing: {short_name(path)}"
            self.rebuild_menu(force=True)
            return

        if LaunchPanel is None:
            # Fallback: launch immediately with recommended params
            self._start_with_params(path, optimal_params(path, self.cfg, self.ram_gb))
            return

        # Close any previous panel
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
        self._notify_now(APP_NAME, "Configure model", short_name(path))

    def _on_panel_launch(self, path: str, params: dict) -> None:
        # Called from panel thread — hop to main work safely
        try:
            self._start_with_params(path, params)
        except Exception as exc:
            with self._lock:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = str(exc)
            self._request_rebuild()
            self._notify_now(APP_NAME, "Failed to start", str(exc))

    def _start_with_params(self, path: str, p: dict) -> None:
        binary = self.cfg.llama_server or discover_llama_server()
        if not binary or not os.access(binary, os.X_OK):
            with self._lock:
                self.rt.state = ServerState.ERROR
                self.rt.error_message = "llama-server binary not found"
            self.rebuild_menu(force=True)
            return

        with self._lock:
            self._ready_gen += 1
            ready_gen = self._ready_gen
            if self.process_alive():
                self._kill_existing_server()

            self.rt.model_path = path
            self.rt.params = dict(p)
            self.rt.error_message = ""
            self.rt.state = ServerState.STARTING
            self.rt.started_at = time.time()
            self._menu_signature = None

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_out = subprocess.DEVNULL
            try:
                self._close_log()
                self._log_fp = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
                self._log_fp.write(
                    f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start {short_name(path)} ---\n"
                )
                self._log_fp.write(f"params: {json.dumps(p)}\n")
                log_out = self._log_fp
            except Exception:
                self._log_fp = None
                log_out = subprocess.DEVNULL

            cmd = self._build_server_cmd(binary, path, p)
            try:
                if self._log_fp:
                    self._log_fp.write("cmd: " + " ".join(cmd) + "\n")
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
                self._do_rebuild(force=True)
                self._notify_now(APP_NAME, "Failed to start", str(exc))
                return

        self.rebuild_menu(force=True)
        self._notify_now(APP_NAME, "Starting", short_name(path))
        self._spawn_ready_watcher(ready_gen)

    def _build_server_cmd(self, binary: str, path: str, p: dict) -> list:
        """Assemble llama-server argv from launch-panel params."""
        cmd = [
            binary,
            "--model", path,
            "--host", self.cfg.host,
            "--port", str(self.cfg.port),
            "-ngl", str(int(p.get("ngl", 999))),
            "-c", str(int(p.get("ctx", 8192))),
            "-t", str(int(p.get("threads", 4))),
            "-b", str(int(p.get("batch", 512))),
            "-n", str(int(p.get("n_predict", -1))),
            "-s", str(int(p.get("seed", -1))),
            "--temp", str(float(p.get("temperature", 0.7))),
            "--top-p", str(float(p.get("top_p", 0.95))),
            "--top-k", str(int(p.get("top_k", 40))),
            "--min-p", str(float(p.get("min_p", 0.05))),
            "--repeat-penalty", str(float(p.get("repeat_penalty", 1.1))),
            "--repeat-last-n", str(int(p.get("repeat_last_n", 64))),
        ]
        return cmd

    def _spawn_ready_watcher(self, ready_gen: int) -> None:
        def watch():
            deadline = time.time() + READY_TIMEOUT_S
            while time.time() < deadline:
                with self._lock:
                    if ready_gen != self._ready_gen:
                        return
                    if self.rt.state != ServerState.STARTING:
                        return
                    if self.rt.proc is not None and self.rt.proc.poll() is not None:
                        code = self.rt.proc.returncode
                        self.rt.state = ServerState.ERROR
                        self.rt.error_message = f"Server exited (code {code})"
                        self.rt.proc = None
                        self._close_log()
                        self._force_rebuild = True
                        self._menu_signature = None
                        self._pending_notify = (
                            APP_NAME,
                            f"Server exited (code {code})",
                            "Check Tools → Open Server Log",
                        )
                        return
                if self.health_ok():
                    with self._lock:
                        if ready_gen != self._ready_gen or self.rt.state != ServerState.STARTING:
                            return
                        self.rt.state = ServerState.RUNNING
                        model = short_name(self.rt.model_path or "")
                        self._force_rebuild = True
                        self._menu_signature = None
                        self._pending_notify = (APP_NAME, "Ready", model)
                    return
                time.sleep(READY_POLL_INTERVAL_S)

            with self._lock:
                if ready_gen != self._ready_gen:
                    return
                if self.rt.state == ServerState.STARTING:
                    self.rt.state = ServerState.ERROR
                    self.rt.error_message = "Timed out waiting for readiness"
                    self._kill_existing_server()
                    self._force_rebuild = True
                    self._menu_signature = None
                    self._pending_notify = (
                        APP_NAME,
                        "Start timed out",
                        "Check Tools → Open Server Log",
                    )

        threading.Thread(target=watch, name="llama-ready", daemon=True).start()

    def stop_server(self, _):
        with self._lock:
            self._ready_gen += 1
            self._kill_existing_server()
            self.rt.state = ServerState.STOPPED
            self.rt.model_path = None
            self.rt.params = {}
            self.rt.error_message = ""
            self._menu_signature = None
        self.rebuild_menu(force=True)
        self._notify_now(APP_NAME, "Stopped", "")

    def copy_url(self, _):
        copy_to_clipboard(self.api_url)
        self._notify_now(APP_NAME, "Copied", self.api_url)

    def open_webui(self, _):
        # Chat UI must open in Safari/Chrome — IDE browsers show a blank page.
        if self.rt.state != ServerState.RUNNING:
            self._notify_now(APP_NAME, "Server is off", "Start a model first")
            return
        if not self.health_ok():
            self._notify_now(APP_NAME, "Server not ready", "Wait a moment and try again")
            return
        open_url(self.webui_url)
        self._notify_now(APP_NAME, "Opening Chat", "In your web browser")

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
        if not CONFIG_PATH.is_file():
            self.cfg.save()
        open_path(CONFIG_PATH)

    def reload_config(self, _):
        with self._lock:
            self.cfg = AppConfig.load()
            self.cfg.ensure_dirs()
            self._menu_signature = None
        self.rebuild_menu(force=True)
        self._notify_now(APP_NAME, "Config reloaded", str(CONFIG_PATH))

    def refresh_models(self, _):
        with self._lock:
            self._menu_signature = None
        self.rebuild_menu(force=True)
        self._notify_now(APP_NAME, "Model list refreshed", self.cfg.models_dir)

    def toggle_launch_at_login(self, sender):
        new_val = not is_launch_at_login_enabled()
        ok = set_launch_at_login(new_val)
        self.cfg.launch_at_login = new_val and ok
        self.cfg.save()
        sender.state = is_launch_at_login_enabled()
        self._notify_now(
            APP_NAME,
            "Launch at Login " + ("on" if sender.state else "off"),
            "",
        )
        with self._lock:
            self._menu_signature = None
        self.rebuild_menu(force=True)

    def toggle_stop_on_quit(self, sender):
        self.cfg.stop_server_on_quit = not self.cfg.stop_server_on_quit
        self.cfg.save()
        sender.state = self.cfg.stop_server_on_quit
        with self._lock:
            self._menu_signature = None
        self.rebuild_menu(force=True)

    def quit_app(self, _):
        if self.cfg.stop_server_on_quit:
            with self._lock:
                self._ready_gen += 1
                self._kill_existing_server()
        rumps.quit_application()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point — pure AppKit shell (native_app)."""
    from native_app import run as native_run

    native_run()


if __name__ == "__main__":
    main()
