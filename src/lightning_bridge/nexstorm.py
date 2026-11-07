# SPDX-License-Identifier: GPL-3.0-or-later
"""NexStorm on this PC, reading a detector shared by Lightning Monitor on a Mac.

NexStorm loads Boltek's ``LD350_DLL.dll`` and calls the functions in Boltek's LD-350 Programmers
Toolkit. The Lightning Monitor NexStorm Connector (``nexstorm/LD350_DLL.dll`` in the bridge's folder,
built from ``nexstorm/ld350_connector.c``) has the same functions but reads the detector from the network,
so the LD-350 can stay on the Mac's USB while Lightning Monitor and NexStorm both use it.

``install`` puts the connector in NexStorm's folder, keeping each of Boltek's copies as
``LD350_DLL.dll.boltek``; ``restore`` puts them back. The connector reads ``nexstorm-connector.ini`` from
the bridge's settings folder.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from lightning_bridge import APP_NAME
from lightning_bridge.config import Config, config_directory
from lightning_bridge.devices import BOLTEK_DLL_NAME, NEXSTORM_CONNECTOR_MARKER, bundle_directory, is_nexstorm_connector

CONNECTOR_MARKER = NEXSTORM_CONNECTOR_MARKER
BACKUP_SUFFIX = ".boltek"
SETTINGS_NAME = "nexstorm-connector.ini"
LOG_NAME = "nexstorm-connector.log"
#: NexStorm loads the LD350_DLL.dll beside NexStorm.exe (seen on a real install); Boltek's copies in x86 and
#: x64 are replaced too, so no Boltek DLL is left for NexStorm to pick up.
SUBFOLDERS = ("", "x86", "x64")


class NexStormError(Exception):
    def __init__(self, message: str, needs_administrator: bool = False):
        super().__init__(message)
        self.needs_administrator = needs_administrator


def connector_dll() -> Path | None:
    """The connector shipped with the bridge (or named by ``LD350_CONNECTOR_DLL``)."""
    if os.environ.get("LD350_CONNECTOR_DLL"):
        return Path(os.environ["LD350_CONNECTOR_DLL"])
    for folder in (Path(sys.executable).parent, bundle_directory()):
        candidate = folder / "nexstorm" / BOLTEK_DLL_NAME
        if candidate.exists():
            return candidate
    return None


def settings_path() -> Path:
    """Where the connector looks for its settings: always the current user's bridge settings folder."""
    return config_directory() / SETTINGS_NAME


def log_path() -> Path:
    return config_directory() / LOG_NAME


def settings_text(config: Config) -> str:
    return (
        "; %s: NexStorm on this PC reads the detector that Lightning Monitor shares from another computer.\n"
        "; Written by the bridge; change it with \"lightning-bridge nexstorm connect\" or the tray menu.\n"
        "host = %s\nport = %d\ntoken = %s\nallow_settings = %d\nsquelch_scale = %s\n"
        % (APP_NAME, config.nexstorm_host, int(config.nexstorm_port), config.nexstorm_token,
           1 if config.nexstorm_allow_settings else 0, config.nexstorm_squelch_scale)
    )


def write_settings(config: Config, path: Path | None = None) -> Path:
    config.validate()
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(settings_text(config), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def is_connector(path: Path) -> bool:
    return is_nexstorm_connector(path)


def is_nexstorm_folder(folder: Path) -> bool:
    return (folder / "NexStorm.exe").exists()


def _registry_install_locations() -> list[Path]:
    """InstallLocation of every uninstall entry named NexStorm (Inno Setup records one)."""
    if sys.platform != "win32":
        return []
    import winreg

    locations = []
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for root, path in roots:
        try:
            uninstall = winreg.OpenKey(root, path)
        except OSError:
            continue
        with uninstall:
            for index in range(winreg.QueryInfoKey(uninstall)[0]):
                try:
                    with winreg.OpenKey(uninstall, winreg.EnumKey(uninstall, index)) as entry:
                        name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                        if name.lower().startswith("nexstorm"):
                            locations.append(Path(str(winreg.QueryValueEx(entry, "InstallLocation")[0])))
                except OSError:
                    continue
    return locations


def find_nexstorm(configured: str = "") -> Path | None:
    """NexStorm's folder: the configured one, the installer's record, or the usual places."""
    if configured:
        return Path(configured)
    candidates = _registry_install_locations()
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(variable):
            base = Path(os.environ[variable])
            candidates += [base / "Astrogenic" / "NexStorm", base / "Astrogenic" / "NexStormLite", base / "NexStorm"]
    # NexStorm's installer defaults to C:\Astrogenic\NexStorm.
    candidates += [Path("C:/Astrogenic/NexStorm"), Path("C:/NexStorm")]
    return next((folder for folder in candidates if is_nexstorm_folder(folder)), None)


def targets(folder: Path) -> list[Path]:
    """Every LD350_DLL.dll NexStorm might load: one beside NexStorm.exe, and Boltek's copies in x86 / x64."""
    found = [folder / BOLTEK_DLL_NAME]
    for name in SUBFOLDERS[1:]:
        dll = folder / name / BOLTEK_DLL_NAME
        if dll.exists() or backup_of(dll).exists():
            found.append(dll)
    return found


def backup_of(dll: Path) -> Path:
    return dll.with_name(dll.name + BACKUP_SUFFIX)


def state(folder: Path) -> str:
    """"installed", "not installed" or "partly installed"."""
    installed = [is_connector(dll) for dll in targets(folder)]
    if all(installed):
        return "installed"
    return "partly installed" if any(installed) else "not installed"


def _permission_error(folder: Path, error: OSError) -> NexStormError:
    # Windows error 32: the file is in use, so NexStorm is running; administrator rights won't help.
    if getattr(error, "winerror", None) == 32:
        return NexStormError("NexStorm is using %s: quit NexStorm first" % BOLTEK_DLL_NAME)
    return NexStormError("Can't change %s: %s (it may need administrator rights)" % (folder, error.strerror or error),
                         needs_administrator=True)


def install(folder: Path, connector: Path | None = None) -> list[str]:
    """Puts the connector in NexStorm's folder, keeping Boltek's copies; returns what was done."""
    connector = connector or connector_dll()
    if connector is None or not connector.exists():
        raise NexStormError("The NexStorm connector (nexstorm\\%s) is missing from the bridge's folder" % BOLTEK_DLL_NAME)
    if not is_connector(connector):
        raise NexStormError("%s is not the Lightning Monitor NexStorm Connector" % connector)
    if not is_nexstorm_folder(folder):
        raise NexStormError("NexStorm.exe is not in %s; set nexstorm_folder" % folder)
    done = []
    try:
        for dll in targets(folder):
            backup = backup_of(dll)
            # A Boltek DLL here (even after an earlier install, if NexStorm was reinstalled) is the one to keep.
            if dll.exists() and not is_connector(dll):
                os.replace(dll, backup)
                done.append("Kept Boltek's %s as %s" % (dll, backup.name))
            shutil.copyfile(connector, dll)
            done.append("Installed the connector as %s" % dll)
    except PermissionError as error:
        raise _permission_error(folder, error) from None
    return done


def restore(folder: Path) -> list[str]:
    """Puts Boltek's DLLs back, and removes connector copies that replaced nothing."""
    done = []
    try:
        for dll in targets(folder):
            backup = backup_of(dll)
            if backup.exists():
                os.replace(backup, dll)
                done.append("Restored Boltek's %s" % dll)
            elif is_connector(dll):
                dll.unlink()
                done.append("Removed the connector from %s" % dll)
    except PermissionError as error:
        raise _permission_error(folder, error) from None
    return done or ["Boltek's driver was already in place"]


def bridge_command() -> list[str]:
    """The console bridge, to run a ``nexstorm`` command in another process."""
    if getattr(sys, "frozen", False):
        console = Path(sys.executable).with_name("lightning-bridge.exe")
        return [str(console if console.exists() else Path(sys.executable))]
    return [sys.executable, "-m", "lightning_bridge"]


def run_elevated(arguments: list[str], timeout_seconds: int = 120) -> int:
    """Windows: runs ``lightning-bridge <arguments>`` as administrator (Windows asks first), waits for it
    and returns its exit code."""
    import ctypes
    import subprocess
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess = 0x40
    command = bridge_command() + list(arguments)
    info = ShellExecuteInfo(cbSize=ctypes.sizeof(ShellExecuteInfo), fMask=see_mask_nocloseprocess,
                            lpVerb="runas", lpFile=command[0], lpParameters=subprocess.list2cmdline(command[1:]),
                            nShow=0)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise NexStormError("Administrator rights were not given")
    kernel32 = ctypes.windll.kernel32
    try:
        kernel32.WaitForSingleObject(info.hProcess, timeout_seconds * 1000)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def describe(config: Config) -> list[str]:
    """Human-readable lines for ``lightning-bridge nexstorm status``."""
    folder = find_nexstorm(config.nexstorm_folder)
    lines = ["NexStorm: %s" % (folder or "not found (set nexstorm_folder)")]
    if folder:
        lines.append("Connector: %s" % state(folder))
        for dll in targets(folder):
            kind = "connector" if is_connector(dll) else "Boltek's" if dll.exists() else "none"
            lines.append("  %s: %s" % (dll, kind))
    connector = connector_dll()
    lines.append("Connector in the bridge: %s" % (connector or "missing"))
    if config.nexstorm_host:
        lines.append("Detector: %s:%d%s" % (config.nexstorm_host, config.nexstorm_port, " (token set)" if config.nexstorm_token else ""))
    else:
        lines.append("Detector: not set (lightning-bridge nexstorm connect HOST)")
    lines.append("NexStorm may change the detector's settings: %s" % ("yes" if config.nexstorm_allow_settings else "no"))
    lines.append("Settings: %s" % settings_path())
    lines.append("Log: %s" % log_path())
    return lines
