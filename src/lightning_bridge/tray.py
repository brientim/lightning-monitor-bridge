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
from lightning_bridge import nexstorm, service
from lightning_bridge.discovery import SharedDetectorBrowser, mdns_host_name
from lightning_bridge.icon import bolt_image
from lightning_bridge.serial_relay import BackgroundRelay, RelayStatus
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
        # NexStorm runs only on Windows, so only there does the tray offer to feed it a Mac's detector.
        self.offers_nexstorm = sys.platform == "win32"
        self.nexstorm_folder = None
        self.nexstorm_state = ""
        self.detectors = SharedDetectorBrowser(lambda: self.on_ui_thread(self.refresh))
        self.relay_status = RelayStatus()
        self.relay = BackgroundRelay(self.config, self.on_relay_status)
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
            item("NexStorm", self.nexstorm_menu(), visible=self.offers_nexstorm),
            item("Open at Login", self.toggle_login, checked=lambda _: service.status().startswith("Installed")),
            item("Settings…", lambda: open_file(self.config_path)),
            item("Reload Settings", self.reload),
            item("Show Log", lambda: open_file(settings.log_path(self.config_path))),
            self.pystray.Menu.SEPARATOR,
            item("Quit", self.quit),
        )

    # NexStorm reading a detector shared by Lightning Monitor on a Mac

    def nexstorm_menu(self):
        item = self.pystray.MenuItem
        return self.pystray.Menu(
            item(lambda _: self.nexstorm_summary(), None, enabled=False),
            item(lambda _: "LD-250 mode on %s: %s" % (self.config.nexstorm_com_port, self.relay_status.message), None,
                 enabled=False, visible=lambda _: bool(self.config.nexstorm_com_port)),
            item("Read the Detector From", self.pystray.Menu(self.detector_items)),
            item("Set Up NexStorm", lambda: self.run_nexstorm_action("install"),
                 enabled=lambda _: self.nexstorm_folder is not None and self.nexstorm_state != "installed"),
            item("Restore Boltek's Driver", lambda: self.run_nexstorm_action("restore"),
                 enabled=lambda _: self.nexstorm_folder is not None and self.nexstorm_state != "not installed"),
            item("Let NexStorm Change Detector Settings", self.toggle_nexstorm_settings,
                 checked=lambda _: self.config.nexstorm_allow_settings),
            item("Show NexStorm Log", lambda: open_file(nexstorm.log_path()),
                 enabled=lambda _: nexstorm.log_path().exists()),
        )

    def on_relay_status(self, status: RelayStatus) -> None:
        self.relay_status = status
        self.on_ui_thread(self.refresh)

    def start_relay(self) -> None:
        """NexStorm's LD-250 mode reads the chosen detector through the virtual COM port, when one is set."""
        if self.offers_nexstorm and self.config.nexstorm_com_port and self.config.nexstorm_host:
            self.relay = BackgroundRelay(self.config, self.on_relay_status)
            self.relay.start()

    def nexstorm_summary(self) -> str:
        if self.nexstorm_folder is None:
            return "NexStorm not found (set nexstorm_folder in Settings…)"
        if self.nexstorm_state != "installed":
            return "NexStorm: not set up" if self.nexstorm_state == "not installed" else "NexStorm: partly set up"
        if not self.config.nexstorm_host:
            return "NexStorm: choose the detector to read"
        if self.config.nexstorm_host == "127.0.0.1":
            return "NexStorm reads this PC's detector"
        return "NexStorm reads %s:%d" % (self.config.nexstorm_host, self.config.nexstorm_port)

    def detector_items(self):
        item = self.pystray.MenuItem
        found = self.detectors.detectors()
        this_pc = ("127.0.0.1", int(self.config.port))
        items = [
            item("This PC's Detector (shared by this bridge)", self.read_this_pc,
                 checked=lambda _: (self.config.nexstorm_host, int(self.config.nexstorm_port)) == this_pc, radio=True),
            self.pystray.Menu.SEPARATOR,
        ] + [
            item("%s (%s)" % (detector.name, detector.host), self.nexstorm_reader(detector),
                 checked=lambda _, detector=detector: (detector.host, detector.port) == (self.config.nexstorm_host, self.config.nexstorm_port),
                 radio=True)
            for detector in found
        ]
        if not found:
            items.append(item("Looking for shared detectors…", None, enabled=False))
        chosen = (self.config.nexstorm_host, int(self.config.nexstorm_port))
        if self.config.nexstorm_host and chosen != this_pc and all((detector.host, detector.port) != chosen for detector in found):
            items.append(item("%s:%d" % (self.config.nexstorm_host, self.config.nexstorm_port), None, checked=lambda _: True, radio=True, enabled=False))
        items.append(item("Enter a Host or Token: Settings…", lambda: open_file(self.config_path)))
        return items

    def nexstorm_reader(self, detector):
        def choose() -> None:
            self.config.nexstorm_host, self.config.nexstorm_port = detector.host, detector.port
            self.save_nexstorm_settings()
            logger.info("NexStorm will read the detector shared by %s (%s:%d)", detector.name, detector.host, detector.port)
            if detector.requires_token and not self.config.nexstorm_token:
                logger.warning("%s needs a token: set nexstorm_token in Settings…, then Reload Settings", detector.name)
            if self.nexstorm_state == "installed":
                logger.info("Restart NexStorm to read the new detector")
        return choose

    def read_this_pc(self) -> None:
        """NexStorm reads the detector this bridge shares from the PC's own USB, beside Lightning Monitor's viewers."""
        self.config.nexstorm_host, self.config.nexstorm_port = "127.0.0.1", int(self.config.port)
        self.config.nexstorm_token = self.config.effective_token or ""
        self.save_nexstorm_settings()
        logger.info("NexStorm will read this PC's detector, shared by the bridge on port %d", self.config.port)
        if not self.bridge.running:
            logger.warning("Turn Sharing on: NexStorm reads this PC's detector through it")

    def toggle_nexstorm_settings(self) -> None:
        self.config.nexstorm_allow_settings = not self.config.nexstorm_allow_settings
        self.save_nexstorm_settings()
        logger.info("NexStorm %s change the detector's settings (from NexStorm's next start)",
                    "may" if self.config.nexstorm_allow_settings else "may not")

    def save_nexstorm_settings(self) -> None:
        try:
            settings.save(self.config, self.config_path)
            if self.config.nexstorm_host:
                nexstorm.write_settings(self.config)
        except (OSError, ValueError) as error:
            logger.error("NexStorm settings not saved: %s", error)
        self.on_ui_thread(self.refresh)

    def update_nexstorm_state(self) -> None:
        if not self.offers_nexstorm:
            return
        self.nexstorm_folder = nexstorm.find_nexstorm(self.config.nexstorm_folder)
        self.nexstorm_state = nexstorm.state(self.nexstorm_folder) if self.nexstorm_folder else ""

    def run_nexstorm_action(self, action: str) -> None:
        threading.Thread(target=self.nexstorm_action, args=(action,), name="nexstorm", daemon=True).start()

    def nexstorm_action(self, action: str) -> None:
        """Installs or restores the connector, asking Windows for administrator rights when NexStorm's
        folder needs them."""
        folder = self.nexstorm_folder
        if folder is None:
            return
        try:
            if self.config.nexstorm_host:
                nexstorm.write_settings(self.config)
            for line in (nexstorm.install if action == "install" else nexstorm.restore)(folder):
                logger.info("NexStorm: %s", line)
        except nexstorm.NexStormError as error:
            if not error.needs_administrator:
                logger.error("NexStorm: %s", error)
            else:
                try:
                    code = nexstorm.run_elevated(["nexstorm", action, "--folder", str(folder), "--config", str(self.config_path)])
                    if code != 0:
                        logger.error("NexStorm: %s (as administrator, exit code %d)", error, code)
                except nexstorm.NexStormError as elevation_error:
                    logger.error("NexStorm: %s", elevation_error)
        self.update_nexstorm_state()
        logger.info("NexStorm: connector %s in %s", self.nexstorm_state, folder)
        if action == "install" and self.nexstorm_state == "installed":
            logger.info("Quit NexStorm (if it's running) and start it again")
        self.on_ui_thread(self.refresh)

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
        if self.offers_nexstorm and self.config.nexstorm_host:
            try:
                nexstorm.write_settings(self.config)
            except OSError as error:
                logger.error("NexStorm settings not saved: %s", error)
        self.relay.stop()
        self.relay_status = RelayStatus()
        self.start_relay()
        self.update_nexstorm_state()
        self.on_ui_thread(self.refresh)

    def quit(self) -> None:
        self.bridge.stop()
        self.relay.stop()
        self.detectors.stop()
        self.icon.stop()

    def run(self) -> None:
        def setup(icon) -> None:
            icon.visible = True
            self.bridge.start()
            if self.offers_nexstorm:
                self.update_nexstorm_state()
                self.detectors.start()
                self.start_relay()
                self.refresh()

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
