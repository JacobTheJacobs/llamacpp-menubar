#!/usr/bin/env python3
"""
Pure AppKit menu-bar shell for Llama Menu.

Creates NSStatusItem on the main thread in applicationDidFinishLaunching.
This is the reliable way to show a menu bar icon with Python on macOS.
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
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSTimer,
)
from PyObjCTools import AppHelper  # noqa: E402

import llama_core as core  # noqa: E402


def _log(msg: str) -> None:
    try:
        core.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(core.LOG_DIR / "ui.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


class MenuTarget(NSObject):
    """ObjC target for NSMenuItem actions."""

    controller = None

    def initWithController_(self, controller):
        self = objc_super(MenuTarget, self).init()
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


def objc_super(cls, self):
    return super(cls, self)


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
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

            self.controller = core.LlamaController(ui=self)
            self.menu_target = MenuTarget.alloc().initWithController_(self.controller)

            # Create status item on main thread — THIS is what appears in the menu bar
            bar = NSStatusBar.systemStatusBar()
            self.status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
            # Prevent GC of the status item
            self._retain = [self.status_item, self.menu_target, self.controller]

            btn = self.status_item.button()
            if btn is None:
                _log("FATAL: button() is None on status item")
                # Fallback length square
                self.status_item = bar.statusItemWithLength_(24.0)
                btn = self.status_item.button()
                self._retain[0] = self.status_item

            if btn is not None:
                btn.setTitle_("Llama")
                btn.setToolTip_("Llama Menu — click to open")
                _log(f"button title now={btn.title()!r}")
            else:
                self.status_item.setTitle_("Llama")
                _log("used setTitle_ fallback")

            self.controller.bind_status_item(self.status_item)
            self.controller.rebuild_menu()
            self.controller.apply_appearance()

            self.timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    2.0, self, "tick:", None, True
                )
            )
            self._retain.append(self.timer)
            _log("native AppKit menu bar READY — look for 'Llama' in the menu bar")

            self.controller.maybe_welcome()
        except Exception:
            _log("FATAL didFinishLaunching:\n" + traceback.format_exc())

    def tick_(self, timer):
        try:
            if self.controller:
                self.controller.tick()
        except Exception:
            _log("tick error:\n" + traceback.format_exc())

    def applicationWillTerminate_(self, notification):
        try:
            if self.controller:
                self.controller.shutdown()
        except Exception:
            pass


# Module-level strong refs (NSApplication proxy won't accept arbitrary attrs)
_KEEP_ALIVE: list = []


def run() -> None:
    core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    core.LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"=== native start pid={os.getpid()} py={sys.executable} ===")

    lock = core.SingleInstance(core.PID_LOCK_PATH)
    if not lock.acquire():
        _log("already running")
        try:
            from Foundation import NSUserNotification, NSUserNotificationCenter

            n = NSUserNotification.alloc().init()
            n.setTitle_("Llama Menu")
            n.setInformativeText_(
                "Already running — find “Llama” in the menu bar (top right)"
            )
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(
                n
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

    _log("entering runEventLoop")
    AppHelper.runEventLoop()


if __name__ == "__main__":
    run()
