# SPDX-License-Identifier: GPL-3.0-or-later
"""Detector backends.

Every backend has the same small interface, used by the bridge's detector thread:
``model``, ``driver``, ``line_ending``, ``read()`` (returns bytes, possibly empty), ``write(bytes)``,
``close()``, ``keep_alive_commands(tick)`` (sent every half second) and ``startup_commands()``.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
from pathlib import Path

LD350_VID = 0x0403
LD350_PID = 0xF241
BOLTEK_DLL_NAME = "LD350_DLL.dll"


class DetectorError(Exception):
    pass


def bundle_directory() -> Path:
    """Where bundled files live: next to the frozen executable, or this package's folder."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def bundled_libusb() -> str | None:
    names = {"win32": ["libusb-1.0.dll"], "darwin": ["libusb-1.0.0.dylib", "libusb-1.0.dylib"]}.get(
        sys.platform, ["libusb-1.0.so.0", "libusb-1.0.so"]
    )
    for folder in (bundle_directory(), Path(sys.executable).parent):
        for name in names:
            candidate = folder / name
            if candidate.exists():
                return str(candidate)
    return None


def use_libusb(path: str | None) -> None:
    """Makes pyusb load libusb from ``path`` (or the bundled copy); later backends reuse it."""
    path = path or bundled_libusb()
    if not path:
        return
    import usb.backend.libusb1

    usb.backend.libusb1.get_backend(find_library=lambda _: path)


def keep_alive_commands(tick: int) -> list[bytes]:
    # KA at least once a second; STATUS every second (the LTS-3 card suppresses unsolicited status).
    commands = [b"KA"]
    if tick % 2 == 0:
        commands.append(b"STATUS")
    return commands


class LD350Device:
    """Channel A of the LD-350's FTDI FT2232 through libusb (WinUSB on Windows), in FIFO mode.

    Relays everything, including binary waveform captures (``RAW 1``).
    """

    line_ending = b"\r\n"
    model = "LD-350"
    driver = "libusb"
    waveforms = True
    registered = False

    def __init__(self, libusb_path: str | None = None):
        from pyftdi.ftdi import Ftdi

        use_libusb(libusb_path)
        if not LD350Device.registered:
            Ftdi.add_custom_product(LD350_VID, LD350_PID, "ld350")
            LD350Device.registered = True
        self.ftdi = Ftdi()
        self.ftdi.open_from_url("ftdi://0x%04x:0x%04x/1" % (LD350_VID, LD350_PID))
        self.ftdi.set_latency_timer(16)
        self.ftdi.purge_buffers()
        self.lock = threading.Lock()

    def read(self) -> bytes:
        return self.ftdi.read_data_bytes(4096, attempt=1) or b""

    def write(self, data: bytes) -> None:
        with self.lock:
            self.ftdi.write_data(data)

    def close(self) -> None:
        self.ftdi.close()

    def keep_alive_commands(self, tick: int) -> list[bytes]:
        return keep_alive_commands(tick)

    def startup_commands(self) -> list[bytes]:
        return [b"RAW 1", b"VER"]


class LD350BoltekDevice:
    """An LD-350 through Boltek's own Windows driver and LD350_DLL.dll.

    NexStorm and other Boltek software keep working with this driver. The DLL delivers decoded
    messages but not the raw stream, so waveform captures are discarded and strikes are unclassified.
    The DLL is 32-bit, so this needs the 32-bit (x86) Windows build.
    """

    line_ending = b"\r\n"
    model = "LD-350"
    driver = "boltek"
    waveforms = False
    message_size = 50
    capture_size = 6400
    # C++ decorated names exported by LD350_DLL.dll (cdecl).
    names = {
        "open": "?LD350_Open@@YAHXZ",
        "close": "?LD350_Close@@YAHXZ",
        "keep_alive": "?LD350_KeepAlive@@YAHXZ",
        "command": "?LD350_Command@@YAHPAD@Z",
        "message_ready": "?LD350_MessageReady@@YA_NXZ",
        "get_message": "?LD350_GetMessageData@@YAXPAULD350_MessageDataType@@@Z",
        "capture_ready": "?LD350_CaptureReady@@YA_NXZ",
        "get_capture": "?LD350_GetCaptureData@@YAXPAULD350_CaptureDataType@@@Z",
    }

    def __init__(self, dll_path: str | None = None, loader=None):
        if loader is None:
            if sys.platform != "win32":
                raise DetectorError("Boltek's driver is only available on Windows")
            if struct.calcsize("P") != 4:
                raise DetectorError("Boltek's LD350_DLL.dll is 32-bit; use the 32-bit (x86) Windows build")
            loader = ctypes.CDLL
        path = dll_path or find_boltek_dll()
        if not path:
            raise DetectorError(
                "%s not found. Download Boltek's LD-350 Programmers Toolkit and put the DLL next to the bridge"
                % BOLTEK_DLL_NAME
            )
        if os.path.isdir(os.path.dirname(path)) and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(os.path.dirname(os.path.abspath(path)))
        dll = loader(path)
        self.call = {key: dll[name] for key, name in self.names.items()}
        for key in ("open", "close", "keep_alive", "command"):
            self.call[key].restype = ctypes.c_int
        for key in ("message_ready", "capture_ready"):
            self.call[key].restype = ctypes.c_bool
        self.call["command"].argtypes = [ctypes.c_char_p]
        self.call["get_message"].argtypes = [ctypes.c_char_p]
        self.call["get_capture"].argtypes = [ctypes.c_char_p]
        self.call["get_message"].restype = None
        self.call["get_capture"].restype = None
        self.lock = threading.Lock()
        with self.lock:
            if self.call["open"]() != 1:
                raise DetectorError("LD350_Open failed: is NexStorm running, or the detector unplugged?")
            self.call["keep_alive"]()

    def read(self) -> bytes:
        lines = []
        with self.lock:
            capture = ctypes.create_string_buffer(self.capture_size)
            while self.call["capture_ready"]():
                self.call["get_capture"](capture)
            message = ctypes.create_string_buffer(self.message_size)
            while self.call["message_ready"]() and len(lines) < 100:
                self.call["get_message"](message)
                text = message.raw.split(b"\0", 1)[0].rstrip(b"\r\n")
                if text:
                    lines.append(text + self.line_ending)
        if not lines:
            time.sleep(0.01)
        return b"".join(lines)

    def write(self, data: bytes) -> None:
        for raw in data.replace(b"\r", b"\n").split(b"\n"):
            command = raw.strip()
            if not command:
                continue
            with self.lock:
                if command.upper() == b"KA":
                    self.call["keep_alive"]()
                else:
                    self.call["command"](command)

    def close(self) -> None:
        with self.lock:
            self.call["close"]()

    def keep_alive_commands(self, tick: int) -> list[bytes]:
        return keep_alive_commands(tick)

    def startup_commands(self) -> list[bytes]:
        return [b"VER"]


class LD250Device:
    """An LD-250 (or EFM-100) on an RS-232 port, usually through a USB-serial adapter, 9600 8N1."""

    line_ending = b"\r"
    model = "LD-250"
    driver = "serial"
    waveforms = False

    def __init__(self, path: str):
        import serial

        self.port = serial.Serial(path, 9600, timeout=0.1)
        self.lock = threading.Lock()

    def read(self) -> bytes:
        return self.port.read(4096)

    def write(self, data: bytes) -> None:
        with self.lock:
            self.port.write(data)

    def close(self) -> None:
        self.port.close()

    def keep_alive_commands(self, tick: int) -> list[bytes]:
        return []

    def startup_commands(self) -> list[bytes]:
        return []


def find_boltek_dll() -> str | None:
    for folder in (Path(sys.executable).parent, bundle_directory(), Path.cwd()):
        candidate = folder / BOLTEK_DLL_NAME
        if candidate.exists():
            return str(candidate)
    return None


def open_device(config):
    """Opens the configured detector. With ``ld350_driver`` "auto", Windows tries libusb (WinUSB)
    first and then Boltek's driver."""
    if config.detector == "ld250":
        if not config.serial_port:
            raise DetectorError("Set the LD-250's serial port (serial_port), e.g. COM3 or /dev/ttyUSB0")
        return LD250Device(config.serial_port)
    driver = config.ld350_driver
    if driver == "boltek":
        return LD350BoltekDevice(config.boltek_dll or None)
    if driver == "libusb" or sys.platform != "win32":
        return LD350Device(config.libusb or None)
    try:
        return LD350Device(config.libusb or None)
    except Exception as libusb_error:  # noqa: BLE001 - fall back to Boltek's driver
        try:
            return LD350BoltekDevice(config.boltek_dll or None)
        except DetectorError as boltek_error:
            raise DetectorError("WinUSB: %s; Boltek driver: %s" % (libusb_error, boltek_error)) from None


def describe_detectors(libusb_path: str | None = None) -> list[str]:
    """Human-readable lines about the detectors and serial ports this computer can see."""
    lines = []
    try:
        import usb.core

        use_libusb(libusb_path)
        found = list(usb.core.find(find_all=True, idVendor=LD350_VID, idProduct=LD350_PID))
        lines.append("LD-350 on USB (libusb%s): %s" % (
            " / WinUSB" if sys.platform == "win32" else "",
            "%d found" % len(found) if found else "not found",
        ))
    except Exception as error:  # noqa: BLE001 - libusb missing or not permitted
        lines.append("LD-350 on USB (libusb): cannot check (%s)" % error)
    if sys.platform == "win32":
        dll = find_boltek_dll()
        lines.append("Boltek driver DLL: %s" % (dll or "%s not found" % BOLTEK_DLL_NAME))
    try:
        from serial.tools import list_ports

        ports = sorted(port.device for port in list_ports.comports())
        lines.append("Serial ports (LD-250): %s" % (", ".join(ports) if ports else "none"))
    except Exception as error:  # noqa: BLE001
        lines.append("Serial ports: cannot list (%s)" % error)
    return lines
