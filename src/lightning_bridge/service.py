# SPDX-License-Identifier: GPL-3.0-or-later
"""Start the bridge automatically: launchd on macOS, systemd on Linux, a scheduled task on Windows.

``install`` registers either the headless service (``run``) or the tray app (``tray``). Only one of them
should be installed: both would try to open the same detector.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from lightning_bridge import APP_NAME
from lightning_bridge.config import default_config_path, log_path

LABEL = "au.com.brien.lightning-monitor-bridge"
SYSTEMD_UNIT = "lightning-monitor-bridge.service"
WINDOWS_TASK = APP_NAME
UDEV_RULE_PATH = Path("/etc/udev/rules.d/99-boltek-ld350.rules")
UDEV_RULE = (
    "# Boltek LD-350 (FTDI FT2232, custom product ID): let Lightning Monitor Bridge open it without root.\n"
    'SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="f241", MODE="0666"\n'
)


def program(mode: str, config_path: Path) -> list[str]:
    """The command line that starts the bridge in ``mode`` ("run" or "tray")."""
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        base = [sys.executable, "-m", "lightning_bridge"]
    return base + [mode, "--config", str(config_path)]


# macOS


def launchd_plist(arguments: list[str], log_file: Path, user: str | None = None) -> str:
    items = "\n".join("        <string>%s</string>" % escape(argument) for argument in arguments)
    user_key = "    <key>UserName</key>\n    <string>%s</string>\n" % escape(user) if user else ""
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>%s</string>
    <key>ProgramArguments</key>
    <array>
%s
    </array>
%s    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>%s</string>
    <key>StandardErrorPath</key>
    <string>%s</string>
</dict>
</plist>
""" % (LABEL, items, user_key, escape(str(log_file)), escape(str(log_file)))


def launchd_path(system: bool) -> Path:
    if system:
        return Path("/Library/LaunchDaemons") / ("%s.plist" % LABEL)
    return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % LABEL)


# Linux


def systemd_unit(arguments: list[str], user: str) -> str:
    command = " ".join(_systemd_quote(argument) for argument in arguments)
    return """[Unit]
Description=%s (shares a Boltek detector with Lightning Monitor)
Wants=network-online.target
After=network-online.target

[Service]
User=%s
ExecStart=%s
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""" % (APP_NAME, user, command)


def _systemd_quote(argument: str) -> str:
    if argument and all(character.isalnum() or character in "/._-=:" for character in argument):
        return argument
    return '"%s"' % argument.replace("\\", "\\\\").replace('"', '\\"')


def linux_autostart_entry(arguments: list[str]) -> str:
    command = " ".join(_systemd_quote(argument) for argument in arguments)
    return "[Desktop Entry]\nType=Application\nName=%s\nExec=%s\nX-GNOME-Autostart-enabled=true\n" % (APP_NAME, command)


def linux_autostart_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "autostart" / "lightning-monitor-bridge.desktop"


# Windows


def windows_task_command(arguments: list[str], at_startup: bool) -> list[str]:
    command = subprocess.list2cmdline(arguments)
    schedule = ["/SC", "ONSTART", "/RU", "SYSTEM"] if at_startup else ["/SC", "ONLOGON"]
    return ["schtasks", "/Create", "/TN", WINDOWS_TASK, "/TR", command, *schedule, "/RL", "LIMITED", "/F"]


# Install / uninstall


def install(mode: str = "run", system: bool = False, config_path: Path | None = None) -> str:
    """Registers the bridge to start automatically; returns a description of what was done."""
    config_path = config_path or default_config_path()
    arguments = program(mode, config_path)
    if sys.platform == "darwin":
        path = launchd_path(system)
        user = os.environ.get("SUDO_USER") if system else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist(arguments, log_path(config_path), user), encoding="utf-8")
        domain = "system" if system else "gui/%d" % os.getuid()
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        return "Installed %s; it starts %s." % (path, "at boot" if system else "when you log in")
    if sys.platform == "win32":
        subprocess.run(windows_task_command(arguments, system), check=True)
        return "Created the scheduled task %r; it starts %s." % (WINDOWS_TASK, "at startup" if system else "when you log in")
    if mode == "tray":
        path = linux_autostart_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(linux_autostart_entry(arguments), encoding="utf-8")
        return "Installed %s; the tray app starts when you log in." % path
    if os.geteuid() != 0:
        raise PermissionError("Installing the Linux service needs root: run it with sudo")
    user = os.environ.get("SUDO_USER") or "root"
    unit = Path("/etc/systemd/system") / SYSTEMD_UNIT
    unit.write_text(systemd_unit(arguments, user), encoding="utf-8")
    UDEV_RULE_PATH.write_text(UDEV_RULE, encoding="utf-8")
    subprocess.run(["udevadm", "control", "--reload-rules"], check=False)
    subprocess.run(["udevadm", "trigger"], check=False)
    if user != "root" and shutil.which("usermod"):
        subprocess.run(["usermod", "-a", "-G", "dialout", user], check=False)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", SYSTEMD_UNIT], check=True)
    return "Installed and started %s (runs as %s), and the LD-350 udev rule %s." % (unit, user, UDEV_RULE_PATH)


def uninstall(system: bool = False) -> str:
    if sys.platform == "darwin":
        removed = []
        for path, domain in ((launchd_path(False), "gui/%d" % os.getuid()), (launchd_path(True), "system")):
            if path.exists() and (system or path == launchd_path(False)):
                subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
                path.unlink()
                removed.append(str(path))
        return "Removed %s." % ", ".join(removed) if removed else "Nothing to remove."
    if sys.platform == "win32":
        result = subprocess.run(["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"], capture_output=True, text=True)
        return "Removed the scheduled task." if result.returncode == 0 else "No scheduled task to remove."
    removed = []
    autostart = linux_autostart_path()
    if autostart.exists():
        autostart.unlink()
        removed.append(str(autostart))
    unit = Path("/etc/systemd/system") / SYSTEMD_UNIT
    if unit.exists():
        if os.geteuid() != 0:
            raise PermissionError("Removing the Linux service needs root: run it with sudo")
        subprocess.run(["systemctl", "disable", "--now", SYSTEMD_UNIT], check=False)
        unit.unlink()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        removed.append(str(unit))
    return "Removed %s." % ", ".join(removed) if removed else "Nothing to remove."


def status() -> str:
    if sys.platform == "darwin":
        for path in (launchd_path(False), launchd_path(True)):
            if path.exists():
                return "Installed: %s" % path
        return "Not installed"
    if sys.platform == "win32":
        result = subprocess.run(["schtasks", "/Query", "/TN", WINDOWS_TASK], capture_output=True, text=True)
        return "Installed: scheduled task %r" % WINDOWS_TASK if result.returncode == 0 else "Not installed"
    if (Path("/etc/systemd/system") / SYSTEMD_UNIT).exists():
        result = subprocess.run(["systemctl", "is-active", SYSTEMD_UNIT], capture_output=True, text=True)
        return "Installed: %s (%s)" % (SYSTEMD_UNIT, result.stdout.strip() or "unknown")
    if linux_autostart_path().exists():
        return "Installed: %s" % linux_autostart_path()
    return "Not installed"
