#!/usr/bin/env python3
"""
Pure AppKit shell for Llama Menu.

Uses BOTH:
  1. Menu bar status item (forced visible every tick)
  2. Dock icon + main app menu (so there is always a way to open the app)

On recent macOS, status items from script-hosted apps can be hard to spot or
get tucked into the overflow area — the Dock icon is the reliable fallback.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from AppKit import (  # noqa: E402
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSTimer,
    NSImageLeft,
)
from Foundation import NSMakeSize  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402

import llama_core as core  # noqa: E402

_KEEP_ALIVE: list = []
STATUS_AUTOSAVE = "com.llamamenu.app.statusitem"


def _log(msg: str) -> None:
    try:
        core.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(core.LOG_DIR / "ui.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


class MenuTarget(NSObject):
    controller = None

    def initWithController_(self, controller):
        self = super(MenuTarget, self).init()
        if self is None:
            return None
        self.controller = controller
        return self

    def openLaunchForModel_(self, sender):
        path = sender.representedObject()
        if path and self.controller:
            self.controller.open_launch_panel(str(path))

    def stopServer_(self, sender):
        if self.controller:
            self.controller.stop_server()

    def openChat_(self, sender):
        if self.controller:
            self.controller.open_webui()

    def openLog_(self, sender):
        if self.controller:
            self.controller.open_log()

    def toggleStopOnQuit_(self, sender):
        if self.controller:
            self.controller.toggle_stop_on_quit()

    def quitApp_(self, sender):
        if self.controller:
            self.controller.quit_app()

    def showAbout_(self, sender):
        try:
            from AppKit import NSAlert

            a = NSAlert.alloc().init()
            a.setMessageText_("Llama Menu")
            a.setInformativeText_(
                f"Version {core.app_version()}\n\n"
                "• Menu bar: look for “🦙 Llama” (top-right, near the clock)\n"
                "• Or click this Dock icon → menu\n"
                "• Chat URL: http://127.0.0.1:8180/\n"
            )
            a.addButtonWithTitle_("OK")
            a.runModal()
        except Exception as exc:
            _log(f"about fail: {exc}")


class AppDelegate(NSObject):
    status_item = None
    controller = None
    menu_target = None
    timer = None
    _retain = None

    def applicationDidFinishLaunching_(self, notification):
        try:
            _log("applicationDidFinishLaunching")
            app = NSApplication.sharedApplication()
            # REGULAR = show Dock icon (reliable way to find the app)
            app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

            self.controller = core.LlamaController(ui=self)
            self.menu_target = MenuTarget.alloc().initWithController_(self.controller)

            # ---- Dock / main menu (always available) ----
            self._install_main_menu()

            # ---- Status item ----
            self._install_status_item()

            self.controller.bind_status_item(self.status_item)
            self.controller.rebuild_menu()
            self.controller.apply_appearance()
            self._force_status_visible()

            self.timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    2.0, self, "tick:", None, True
                )
            )
            self._retain = [
                self.status_item,
                self.menu_target,
                self.controller,
                self.timer,
            ]
            _KEEP_ALIVE.extend(self._retain)

            _log("READY — Dock icon + status item 🦙 Llama")

            # Loud notification
            try:
                from Foundation import NSUserNotification, NSUserNotificationCenter

                n = NSUserNotification.alloc().init()
                n.setTitle_("Llama Menu is running")
                n.setInformativeText_(
                    "1) Dock: click “Llama Menu”\n"
                    "2) Menu bar (top-right): “🦙 Llama”\n"
                    "3) If missing, click » on the menu bar"
                )
                NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(
                    n
                )
            except Exception as exc:
                _log(f"notif: {exc}")

            # Also osascript banner (more reliable than NSUserNotification on some OS)
            try:
                import subprocess

                subprocess.Popen(
                    [
                        "osascript",
                        "-e",
                        'display notification "Look for 🦙 Llama top-right OR the Dock icon" '
                        'with title "Llama Menu is running"',
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

            self.controller.maybe_welcome()
        except Exception:
            _log("FATAL didFinishLaunching:\n" + traceback.format_exc())

    def _install_main_menu(self) -> None:
        """App menu in the menu bar when the Dock icon is focused."""
        try:
            main = NSMenu.alloc().init()
            app_menu_item = NSMenuItem.alloc().init()
            app_menu = NSMenu.alloc().initWithTitle_("Llama Menu")
            about = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "About Llama Menu", "showAbout:", ""
            )
            about.setTarget_(self.menu_target)
            app_menu.addItem_(about)
            app_menu.addItem_(NSMenuItem.separatorItem())
            quit_i = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Quit Llama Menu", "quitApp:", "q"
            )
            quit_i.setTarget_(self.menu_target)
            app_menu.addItem_(quit_i)
            app_menu_item.setSubmenu_(app_menu)
            main.addItem_(app_menu_item)
            NSApp.setMainMenu_(main)
            _KEEP_ALIVE.append(main)
            _log("main menu installed")
        except Exception as exc:
            _log(f"main menu fail: {exc}")

    def _install_status_item(self) -> None:
        bar = NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        self.status_item = item
        _KEEP_ALIVE.append(item)

        try:
            # Prevent macOS from "remembering" a hidden state for this item
            if hasattr(item, "setAutosaveName_"):
                item.setAutosaveName_(STATUS_AUTOSAVE)
        except Exception:
            pass

        btn = item.button()
        label = "🦙 Llama"
        if btn is not None:
            btn.setTitle_(label)
            btn.setImage_(None)
            btn.setToolTip_("Llama Menu")
            try:
                btn.setImagePosition_(NSImageLeft)
            except Exception:
                pass
            _log(f"status button title={btn.title()!r} frame={btn.frame()}")
        else:
            item.setTitle_(label)
            _log("status setTitle_ fallback")

        try:
            item.setLength_(100.0)
        except Exception:
            pass

        self._force_status_visible()
        _log("status item installed")

    def _force_status_visible(self) -> None:
        item = self.status_item
        if item is None:
            return
        try:
            if hasattr(item, "setVisible_"):
                item.setVisible_(True)
            vis = item.isVisible() if hasattr(item, "isVisible") else "?"
            _log(f"force visible -> {vis}")
        except Exception as exc:
            _log(f"setVisible fail: {exc}")

    def tick_(self, timer):
        try:
            if self.controller:
                self.controller.tick()
            self._force_status_visible()
        except Exception:
            _log("tick error:\n" + traceback.format_exc())

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
        """Dock icon click — rebuild menu bar item and notify."""
        try:
            _log("dock reopen")
            if self.status_item is None:
                self._install_status_item()
                if self.controller:
                    self.controller.bind_status_item(self.status_item)
                    self.controller.rebuild_menu()
            self._force_status_visible()
            if self.controller:
                self.controller.apply_appearance()
                self.controller.notify(
                    "Llama Menu",
                    "Menu bar: 🦙 Llama (top-right). Or use this Dock icon.",
                )
        except Exception:
            _log("reopen error:\n" + traceback.format_exc())
        return True

    def applicationWillTerminate_(self, notification):
        try:
            if self.controller:
                self.controller.shutdown()
        except Exception:
            pass


def run() -> None:
    core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    core.LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"=== native start pid={os.getpid()} py={sys.executable} ===")

    lock = core.SingleInstance(core.PID_LOCK_PATH)
    if not lock.acquire():
        _log("already running — activate existing")
        # Bring existing instance forward via open
        try:
            import subprocess

            subprocess.run(
                ["/usr/bin/open", "-a", "Llama Menu"],
                check=False,
            )
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    'display notification "Already running — check Dock or 🦙 Llama in menu bar" '
                    'with title "Llama Menu"',
                ]
            )
        except Exception:
            print("Llama Menu is already running.", file=sys.stderr)
        sys.exit(0)

    import atexit

    atexit.register(lock.release)

    app = NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    _KEEP_ALIVE.extend([app, delegate, lock])

    # App icon in Dock
    try:
        icon_path = core.resource_path("AppIcon.icns")
        if not Path(icon_path).is_file():
            icon_path = core.resource_path("icon.icns")
        if Path(icon_path).is_file():
            img = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
            if img is not None:
                app.setApplicationIconImage_(img)
                _KEEP_ALIVE.append(img)
    except Exception as exc:
        _log(f"dock icon: {exc}")

    _log("entering runEventLoop")
    AppHelper.runEventLoop()


if __name__ == "__main__":
    run()
