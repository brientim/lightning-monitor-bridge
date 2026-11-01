# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings, kept as JSON in the platform's usual settings folder."""

from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from lightning_bridge import APP_NAME

DETECTORS = ("ld350", "ld250")
LD350_DRIVERS = ("auto", "libusb", "boltek")


@dataclass
class Config:
    #: "ld350" (USB) or "ld250" (serial)
    detector: str = "ld350"
    #: LD-250 serial port, e.g. COM3, /dev/ttyUSB0 or /dev/cu.usbserial-XXXX
    serial_port: str = ""
    #: LD-350 on Windows: "auto" (WinUSB, then Boltek's driver), "libusb" (WinUSB) or "boltek"
    ld350_driver: str = "auto"
    host: str = "0.0.0.0"
    port: int = 10001
    #: Clients must send "AUTH <token>". Empty: anyone on the network can connect.
    token: str = ""
    #: Announce the bridge with Bonjour so Lightning Monitor lists it under "On this network".
    advertise: bool = True
    #: The name shown in Lightning Monitor; empty uses this computer's name.
    name: str = ""
    #: Paths, only needed when not found automatically.
    libusb: str = ""
    boltek_dll: str = ""

    def validate(self) -> None:
        if self.detector not in DETECTORS:
            raise ValueError("detector must be one of %s" % ", ".join(DETECTORS))
        if self.ld350_driver not in LD350_DRIVERS:
            raise ValueError("ld350_driver must be one of %s" % ", ".join(LD350_DRIVERS))
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("port must be between 1 and 65535")

    @property
    def effective_token(self) -> str | None:
        token = self.token or os.environ.get("LIGHTNING_BRIDGE_TOKEN") or os.environ.get("LD_PROXY_TOKEN")
        return token or None

    @property
    def model(self) -> str:
        return "LD-250" if self.detector == "ld250" else "LD-350"

    def set(self, key: str, value: str) -> None:
        field = next((item for item in fields(self) if item.name == key), None)
        if field is None:
            raise KeyError("unknown setting %r; settings: %s" % (key, ", ".join(item.name for item in fields(self))))
        current = getattr(self, key)
        if isinstance(current, bool):
            converted = value.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(current, int):
            converted = int(value)
        else:
            converted = value
        setattr(self, key, converted)
        self.validate()


def config_directory(home: Path | None = None) -> Path:
    """The settings folder; ``home`` selects another user's (e.g. the user who ran sudo)."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    if sys.platform == "darwin":
        return (home or Path.home()) / "Library" / "Application Support" / APP_NAME
    if home is None and os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]) / "lightning-monitor-bridge"
    return (home or Path.home()) / ".config" / "lightning-monitor-bridge"


def default_config_path() -> Path:
    if os.environ.get("LIGHTNING_BRIDGE_CONFIG"):
        return Path(os.environ["LIGHTNING_BRIDGE_CONFIG"])
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sys.platform != "win32" and os.geteuid() == 0:
        return config_directory(Path(os.path.expanduser("~" + sudo_user))) / "config.json"
    return config_directory() / "config.json"


def log_path(config_path: Path | None = None) -> Path:
    return (config_path or default_config_path()).parent / "bridge.log"


def load(path: Path | None = None) -> Config:
    path = path or default_config_path()
    config = Config()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        known = {item.name for item in fields(Config)}
        for key, value in data.items():
            if key in known:
                setattr(config, key, value)
    config.validate()
    return config


def save(config: Config, path: Path | None = None) -> Path:
    config.validate()
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def new_token() -> str:
    return secrets.token_urlsafe(24)
