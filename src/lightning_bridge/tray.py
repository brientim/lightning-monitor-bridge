# SPDX-License-Identifier: GPL-3.0-or-later
"""Tray (Windows, Linux) and menu-bar (macOS) app: runs the bridge and shows its status."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from lightning_bridge import APP_NAME, __version__
from lightning_bridge import config as settings
from lightning_bridge import service
from lightning_bridge.discovery import mdns_host_name
from lightning_bridge.icon import bolt_image
from lightning_bridge.server import BackgroundBridge, Status

logger = logging.getLogger("lightning_bridge")


def open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opening the user's own settings or log
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-t", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def describe(status: Status) -> str:
    if not status.running:
        return status.message
    if not status.detector_connected:
        return "Waiting for the detector"
    clients = "1 client" if status.clients == 1 else "%d clients" % status.clients
    return "%s connected · %s" % (status.detector, clients)


class TrayApp:
    def __init__(self, config_path: Path):
        import pystray

        self.pystray = pystray
        self.config_path = config_path
        self.config = settings.load(config_path)
        if not config_path.exists():
            settings.save(self.config, config_path)
        self.status = Status()
        self.lock = threading.Lock()
        self.bridge = BackgroundBridge(self.config, self.on_status)
        self.icon = pystray.Icon("lightning-monitor-bridge", bolt_image(64, False), APP_NAME, self.menu())

    def menu(self):
        item = self.pystray.MenuItem
        return self.pystray.Menu(
            item("%s %s" % (APP_NAME, __version__), None, enabled=False),
            item(lambda _: describe(self.status), None, enabled=False),
            item(lambda _: "Address: %s:%d" % (mdns_host_name(), self.config.port), None, enabled=False),
            item(lambda _: "Token: %s" % ("required" if self.config.effective_token else "none (anyone can connect)"), None, enabled=False),
            self.pystray.Menu.SEPARATOR,
            item("Sharing", self.toggle_sharing, checked=lambda _: self.bridge.running),
            item("Open at Login", self.toggle_login, checked=lambda _: service.status().startswith("Installed")),
            item("Settings…", lambda: open_file(self.config_path)),
            item("Reload Settings", self.reload),
            item("Show Log", lambda: open_file(settings.log_path(self.config_path))),
            self.pystray.Menu.SEPARATOR,
            item("Quit", self.quit),
        )

    def on_status(self, status: Status) -> None:
        """Called on the bridge's thread; the icon is changed on the UI thread."""
        with self.lock:
            self.status = status
        self.on_ui_thread(self.refresh)

    def refresh(self) -> None:
        status = self.status
        self.icon.icon = bolt_image(64, status.detector_connected)
        self.icon.title = "%s: %s" % (APP_NAME, describe(status))
        try:
            self.icon.update_menu()
        except Exception:  # noqa: BLE001 - some backends only refresh when the menu opens
            pass

    def on_ui_thread(self, work) -> None:
        # AppKit (macOS) and GTK (Linux) icons may only be changed on their main thread.
        backend = type(self.icon).__module__
        if backend.endswith("_darwin"):
            from PyObjCTools import AppHelper

            AppHelper.callAfter(work)
        elif backend.endswith(("_gtk", "_appindicator")):
            from gi.repository import GLib

            GLib.idle_add(lambda: work() and False)
        else:
            work()

    def toggle_sharing(self) -> None:
        if self.bridge.running:
            self.bridge.stop()
            self.on_status(Status(message="Sharing is off"))
        else:
            self.bridge.start()

    def toggle_login(self) -> None:
        try:
            if service.status().startswith("Installed"):
                logger.info(service.uninstall())
            else:
                logger.info(service.install("tray", False, self.config_path))
        except Exception as error:  # noqa: BLE001
            logger.error("Open at login: %s", error)

    def reload(self) -> None:
        try:
            self.config = settings.load(self.config_path)
        except ValueError as error:
            logger.error("Settings not reloaded: %s", error)
            return
        was_running = self.bridge.running
        self.bridge.stop()
        self.bridge = BackgroundBridge(self.config, self.on_status)
        if was_running:
            self.bridge.start()

    def quit(self) -> None:
        self.bridge.stop()
        self.icon.stop()

    def run(self) -> None:
        def setup(icon) -> None:
            icon.visible = True
            self.bridge.start()

        self.icon.run(setup)


def run_tray(config_path: Path) -> None:
    try:
        app = TrayApp(config_path)
    except ImportError as error:
        sys.exit("The tray app needs pystray and Pillow (pip install 'lightning-monitor-bridge[tray]'): %s" % error)
    app.run()


def main_entry() -> None:
    """``lightning-bridge-tray`` entry point."""
    from lightning_bridge.cli import main

    main(["tray"] + sys.argv[1:])
