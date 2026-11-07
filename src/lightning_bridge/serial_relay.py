# SPDX-License-Identifier: GPL-3.0-or-later
"""NexStorm in LD-250 mode, reading a shared detector through a virtual COM port pair.

NexStorm talks to an LD-250 over a serial port rather than through LD350_DLL.dll. With a virtual null-modem pair
(com0com: e.g. COM11 <-> COM12), the relay connects to the shared detector like any Lightning Monitor viewer and
writes its lines to one end; NexStorm, set to LD-250 on the other end, reads them as from an LD-250. Lines NexStorm
sends go back to the detector, filtered like the NexStorm connector: queries always, setting changes only with
``nexstorm_allow_settings``.

An LD-250 is relayed as it is. An LD-350 is presented as an LD-250: its waveform captures are left out (an LD-250
has none), its ``$WIMSU`` status becomes ``$WIMST``, and squelch values are converted between the LD-250's 0-15 and
the LD-350's 0-100 trigger level (Boltek's LD350.h table).
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, replace

from lightning_bridge.config import Config

logger = logging.getLogger("lightning_bridge")

RETRY_SECONDS = 3
GREETING_SECONDS = 6
SAMPLE_BYTES = 2048
LTS3_BLOCK_BYTES = 167
LAYOUT_DECISION_BYTES = SAMPLE_BYTES + 14
#: Boltek's LD350.h: LD-250 squelch -> LD-350 trigger level; values between are interpolated.
SQUELCH_PAIRS = ((0, 40), (1, 43), (2, 47), (4, 53), (8, 58), (15, 78))
#: Without an argument these act on the detector rather than ask it something.
ACTIONS = {"SAVE", "READ", "FT", "D1", "D2", "RESET"}
LINE_END = re.compile(rb"[\r\n]")


def ld350_trigger_level(squelch: int) -> int:
    if squelch <= 0:
        return 40
    if squelch >= 15:
        return 78
    for (x0, y0), (x1, y1) in zip(SQUELCH_PAIRS, SQUELCH_PAIRS[1:]):
        if squelch <= x1:
            numerator, denominator = (y1 - y0) * (squelch - x0), x1 - x0
            return y0 + (2 * numerator + denominator) // (2 * denominator)
    return 78


def ld250_squelch(level: int) -> int:
    """The LD-250 squelch whose LD-350 trigger level is nearest to ``level``."""
    return min(range(16), key=lambda squelch: (abs(ld350_trigger_level(squelch) - level), squelch))


def nmea(body: str) -> str:
    checksum = 0
    for character in body[1:]:
        checksum ^= ord(character)
    return "%s*%02X" % (body, checksum)


def as_ld250(line: str) -> str | None:
    """An LD-350 line as an LD-250 would send it, or None to leave it out."""
    if line.startswith("$WIMSU,"):
        fields = line.split("*", 1)[0].split(",")[1:5]
        fields += ["0"] * (4 - len(fields))
        return nmea("$WIMST,%s,000.0" % ",".join(fields))
    match = re.match(r":SQ (\d+)", line)
    if match:
        return ":SQUELCH %d (0-15)" % ld250_squelch(int(match.group(1)))
    return line


class TextFramer:
    """Splits the detector's stream into text lines, leaving out binary waveform captures (the same rules as
    Lightning Monitor's LD350StreamFramer)."""

    def __init__(self):
        self.line = bytearray()
        self.capture: bytearray | None = None

    def feed(self, data: bytes) -> list[bytes]:
        lines = []
        for byte in data:
            self._consume(byte, lines)
        return lines

    def _consume(self, byte: int, lines: list[bytes]) -> None:
        if self.capture is not None:
            self.capture.append(byte)
            if len(self.capture) == LAYOUT_DECISION_BYTES and not self._has_timestamp_block():
                leftover = bytes(self.capture[SAMPLE_BYTES + 1:])
                self.capture = None
                for extra in leftover:
                    self._consume(extra, lines)
            elif len(self.capture) == SAMPLE_BYTES + LTS3_BLOCK_BYTES:
                self.capture = None
            return
        if byte in (0x0D, 0x0A):
            if self.line:
                lines.append(bytes(self.line))
                self.line.clear()
        elif byte == 0:
            pass
        elif byte == ord("[") and not self.line:
            self.capture = bytearray()
        elif len(self.line) < 1024:
            self.line.append(byte)

    def _has_timestamp_block(self) -> bool:
        block = self.capture[SAMPLE_BYTES:]
        return block[10:14] == b"@@Ha" or block[0] != ord("]")


@dataclass(frozen=True)
class RelayStatus:
    detector_connected: bool = False
    port_open: bool = False
    message: str = "Stopped"


class SerialRelay:
    """Relays a shared detector to a COM port for NexStorm's LD-250 mode until ``stop()``."""

    def __init__(self, config: Config, open_serial=None, observer=None):
        self.config = config
        self.open_serial = open_serial or self._open_serial
        self.observer = observer
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.socket = None
        self.serial = None
        self.ld350 = False
        self.status = RelayStatus()

    # Status

    def update(self, **changes) -> None:
        self.status = replace(self.status, **changes)
        if self.observer:
            try:
                self.observer(self.status)
            except Exception:  # noqa: BLE001 - an observer must not stop the relay
                logger.exception("Relay status observer failed")

    # Serial side

    def _open_serial(self):
        import serial

        return serial.Serial(self.config.nexstorm_com_port, 9600, timeout=0.2)

    def serial_thread(self) -> None:
        pending = b""
        while not self.stopping.is_set():
            port = self.serial
            if port is None:
                try:
                    port = self.open_serial()
                except Exception as error:  # noqa: BLE001 - the port may not exist yet
                    self.update(port_open=False, message="Can't open %s: %s" % (self.config.nexstorm_com_port, error))
                    self.stopping.wait(RETRY_SECONDS)
                    continue
                with self.lock:
                    self.serial = port
                self.update(port_open=True)
                logger.info("NexStorm relay: opened %s", self.config.nexstorm_com_port)
            try:
                data = port.read(256)
            except Exception as error:  # noqa: BLE001 - the port went away
                logger.warning("NexStorm relay: %s closed: %s", self.config.nexstorm_com_port, error)
                with self.lock:
                    self.serial = None
                self.update(port_open=False)
                try:
                    port.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            if data:
                *complete, pending = LINE_END.split(pending + data)
                for raw in complete:
                    self.from_nexstorm(raw.decode("ascii", "ignore").strip())
                pending = pending[-256:]

    def to_nexstorm(self, line: str) -> None:
        with self.lock:
            port = self.serial
            if port is None:
                return
            try:
                port.write((line + "\r\n").encode("ascii", "ignore"))
            except Exception as error:  # noqa: BLE001 - the serial thread reopens it
                logger.warning("NexStorm relay: can't write to %s: %s", self.config.nexstorm_com_port, error)

    # Commands from NexStorm

    def from_nexstorm(self, line: str) -> None:
        if not line:
            return
        word, _, argument = line.partition(" ")
        word = word.upper()
        if re.fullmatch(r"SQ\d+", word):
            word, argument = "SQ", word[2:]
        if word in ("KA", "STATUS"):
            return
        changes = bool(argument.strip()) or word in ACTIONS
        command = line
        if word == "SQ" and argument.strip().isdigit() and self.ld350:
            # NexStorm's LD-250 mode sends 0-15; an LD-350 takes its trigger level.
            command = "SQ %d" % ld350_trigger_level(int(argument))
        if changes and not self.config.nexstorm_allow_settings:
            logger.info("NexStorm relay: not sent, NexStorm may not change the detector's settings: %s%s",
                        line, " (%s on the LD-350)" % command if command != line else "")
            return
        with self.lock:
            connection = self.socket
        if connection is None:
            return
        try:
            connection.sendall((command + "\r\n").encode("ascii", "ignore"))
            logger.info("NexStorm relay: -> %s", command)
        except OSError as error:
            logger.warning("NexStorm relay: couldn't send %s: %s", command, error)

    # Network side

    def connect(self):
        host, port, token = self.config.nexstorm_host, int(self.config.nexstorm_port), self.config.nexstorm_token
        connection = socket.create_connection((host, port), timeout=5)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if token:
            connection.sendall(("AUTH %s\r\n" % token).encode())
        connection.settimeout(GREETING_SECONDS)
        try:
            first = connection.recv(4096)
        except socket.timeout:
            first = b""
        if first.startswith(b":ERROR"):
            connection.close()
            raise ConnectionError(first.split(b"\n")[0].decode(errors="replace").strip() +
                                  (" (check the token)" if b"unauthorised" in first else ""))
        greeting = first.split(b"\n")[0].decode(errors="replace").strip() if first.startswith(b":SERVER") else ""
        self.ld350 = "LD-350" in greeting
        connection.settimeout(0.5)
        return connection, (first[len(first.split(b"\n")[0]) + 1:] if greeting else first), greeting

    def run(self) -> None:
        """Relays until ``stop()``; reconnects to the detector and reopens the port as needed."""
        self.update(message="Starting")
        serial_thread = threading.Thread(target=self.serial_thread, name="nexstorm-relay-serial", daemon=True)
        serial_thread.start()
        last_error = None
        while not self.stopping.is_set():
            try:
                connection, leftover, greeting = self.connect()
            except Exception as error:  # noqa: BLE001 - report and retry
                if str(error) != last_error:
                    logger.warning("NexStorm relay: can't reach %s:%s: %s", self.config.nexstorm_host, self.config.nexstorm_port, error)
                    last_error = str(error)
                self.update(detector_connected=False, message="Can't reach %s: %s" % (self.config.nexstorm_host, error))
                self.stopping.wait(RETRY_SECONDS)
                continue
            last_error = None
            with self.lock:
                self.socket = connection
            logger.info("NexStorm relay: connected to %s:%s (%s), relaying to %s", self.config.nexstorm_host,
                        self.config.nexstorm_port, greeting or "no greeting", self.config.nexstorm_com_port)
            self.update(detector_connected=True, message="Relaying %s to %s" % (self.config.nexstorm_host, self.config.nexstorm_com_port))
            framer = TextFramer()
            data = leftover
            try:
                while not self.stopping.is_set():
                    for raw in framer.feed(data):
                        line = raw.decode("ascii", "ignore")
                        if line.startswith(":SERVER") or line.startswith(":ERROR"):
                            continue
                        if line.startswith("$WIMSU,"):
                            self.ld350 = True
                        converted = as_ld250(line) if self.ld350 else line
                        if converted:
                            self.to_nexstorm(converted)
                    try:
                        data = connection.recv(8192)
                    except socket.timeout:
                        data = b""
                        continue
                    if not data:
                        raise ConnectionError("the detector's computer closed the connection")
            except (OSError, ConnectionError) as error:
                if not self.stopping.is_set():
                    logger.warning("NexStorm relay: connection lost (%s); reconnecting", error)
                    self.update(detector_connected=False, message="Reconnecting to %s" % self.config.nexstorm_host)
            finally:
                with self.lock:
                    self.socket = None
                try:
                    connection.close()
                except OSError:
                    pass
            self.stopping.wait(RETRY_SECONDS)
        serial_thread.join(RETRY_SECONDS + 1)
        with self.lock:
            port, self.serial = self.serial, None
        if port is not None:
            try:
                port.close()
            except Exception:  # noqa: BLE001
                pass
        self.update(detector_connected=False, port_open=False, message="Stopped")

    def stop(self) -> None:
        self.stopping.set()
        with self.lock:
            connection = self.socket
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class BackgroundRelay:
    """Runs a SerialRelay on its own thread (used by the tray app)."""

    def __init__(self, config: Config, observer=None):
        self.config = config
        self.observer = observer
        self.relay = None
        self.thread = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.relay = SerialRelay(self.config, observer=self.observer)
        self.thread = threading.Thread(target=self.relay.run, name="nexstorm-relay", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.relay is not None:
            self.relay.stop()
        if self.thread is not None:
            self.thread.join(RETRY_SECONDS + 5)
        self.thread = None
