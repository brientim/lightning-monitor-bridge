# SPDX-License-Identifier: GPL-3.0-or-later
"""The bridge: one detector, shared with Lightning Monitor clients over TCP.

Protocol (the same as Lightning Monitor's own sharing):
  * everything the detector sends - text lines and binary waveform captures - is relayed to every
    authenticated client byte for byte;
  * clients send command lines (VER, SQ 49 ...); KA and STATUS are dropped because the bridge keeps the
    detector alive itself; a line longer than 256 bytes is not forwarded and gets ":ERROR line too long";
  * with a token, a client must send "AUTH <token>" within 5 seconds;
  * after authenticating, a client receives ":SERVER <description>", e.g.
    ":SERVER Lightning Monitor Bridge LD-350";
  * up to 16 clients are served; with a token, up to 16 more may be waiting to authenticate, and the
    longest-waiting one is dropped to make room; a client more than 1 MB behind is disconnected.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, replace

from lightning_bridge import APP_NAME
from lightning_bridge.config import Config

AUTH_TIMEOUT = 5
MAX_LINE = 256
MAX_CLIENTS = 16
MAX_PENDING_CLIENTS = MAX_CLIENTS
MAX_QUEUED_BYTES = 1 << 20
RETRY_SECONDS = 5
LINE_END = re.compile(rb"[\r\n]")

logger = logging.getLogger("lightning_bridge")


@dataclass(frozen=True)
class Status:
    running: bool = False
    detector_connected: bool = False
    detector: str = ""
    driver: str = ""
    clients: int = 0
    message: str = "Stopped"


class Bridge:
    def __init__(self, config: Config, open_device=None):
        from lightning_bridge.devices import open_device as default_open

        self.config = config
        self.token = config.effective_token
        self.open_device = open_device or (lambda: default_open(config))
        self.clients = {}
        self.device = None
        self.loop = None
        self.server = None
        self.stopped = None
        self.stopping = threading.Event()
        self.status = Status()
        self.observers = []

    @property
    def description(self) -> str:
        model = self.device.model if self.device else self.config.model
        return "%s %s" % (APP_NAME, model)

    # Status

    def add_observer(self, observer) -> None:
        """``observer(status)`` is called, on any thread, whenever the status changes."""
        self.observers.append(observer)

    def update_status(self, **changes) -> None:
        self.status = replace(self.status, **changes)
        for observer in list(self.observers):
            try:
                observer(self.status)
            except Exception:  # noqa: BLE001 - an observer must not break the bridge
                logger.exception("Status observer failed")

    def update_client_count(self) -> None:
        self.update_status(clients=self.authenticated_count())

    # Detector side (background thread)

    def device_thread(self) -> None:
        while not self.stopping.is_set():
            try:
                device = self.open_device()
            except Exception as error:  # noqa: BLE001 - report and retry any open failure
                logger.info("Detector not available: %s", error)
                self.update_status(detector_connected=False, message="Waiting for the detector: %s" % error)
                self.stopping.wait(RETRY_SECONDS)
                continue
            self.device = device
            logger.info("Connected to the %s (%s)", device.model, device.driver)
            self.update_status(
                detector_connected=True, detector=device.model, driver=device.driver,
                message="Sharing the %s" % device.model,
            )
            for command in device.startup_commands():
                device.write(command + device.line_ending)
            tick = 0
            next_keep_alive = 0.0
            try:
                while not self.stopping.is_set():
                    now = time.monotonic()
                    if now >= next_keep_alive:
                        for command in device.keep_alive_commands(tick):
                            device.write(command + device.line_ending)
                        tick += 1
                        next_keep_alive = now + 0.5
                    data = device.read()
                    if data and self.loop is not None:
                        self.loop.call_soon_threadsafe(self.broadcast, bytes(data))
            except Exception as error:  # noqa: BLE001 - USB unplug, serial error
                logger.warning("Detector connection lost: %s", error)
                self.update_status(detector_connected=False, message="Detector connection lost: %s" % error)
            finally:
                self.device = None
                try:
                    device.close()
                except Exception:  # noqa: BLE001
                    pass
            self.stopping.wait(RETRY_SECONDS)
        self.update_status(detector_connected=False)

    def forward(self, line: str) -> None:
        device = self.device
        if device is None:
            return
        logger.info("-> %s", line)
        try:
            device.write(line.encode("ascii", "ignore") + device.line_ending)
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not send to the detector: %s", error)

    # Network side (asyncio)

    def broadcast(self, data: bytes) -> None:
        for writer, state in list(self.clients.items()):
            if state["authenticated"]:
                self.send(writer, data)

    def send(self, writer, data: bytes) -> None:
        if writer.transport.get_write_buffer_size() >= MAX_QUEUED_BYTES:
            logger.warning("Client too far behind, disconnecting: %s", writer.get_extra_info("peername"))
            self.clients.pop(writer, None)
            writer.transport.abort()
            self.update_client_count()
            return
        try:
            writer.write(data)
        except Exception:  # noqa: BLE001
            self.clients.pop(writer, None)
            self.update_client_count()

    def authenticated_count(self) -> int:
        return sum(1 for state in self.clients.values() if state["authenticated"])

    def make_room_for_pending_client(self) -> None:
        waiting = [writer for writer, state in self.clients.items() if not state["authenticated"]]
        if len(waiting) >= MAX_PENDING_CLIENTS:
            longest_waiting = waiting[0]
            self.clients.pop(longest_waiting, None)
            longest_waiting.close()

    def greeting(self) -> bytes:
        return (":SERVER %s\r\n" % self.description).encode()

    @staticmethod
    def split_lines(state, data: bytes):
        """Splits received bytes into (line, too_long) pairs at CR or LF, keeping at most MAX_LINE bytes pending."""
        *complete, rest = LINE_END.split(state["pending"] + data)
        lines = []
        for raw in complete:
            lines.append((raw, state["too_long"] or len(raw) > MAX_LINE))
            state["too_long"] = False
        if len(rest) > MAX_LINE:
            state["too_long"] = True
            rest = b""
        state["pending"] = rest
        return lines

    def handle_line(self, writer, state, raw: bytes, too_long: bool) -> bool:
        """Acts on one line from a client; returns False when the client must be disconnected."""
        line = raw.decode("ascii", "ignore").strip()
        if not line and not too_long:
            return True
        if not state["authenticated"]:
            if too_long or line != "AUTH %s" % self.token:
                writer.write(b":ERROR unauthorised\r\n")
                return False
            if self.authenticated_count() >= MAX_CLIENTS:
                writer.write(b":ERROR too many clients\r\n")
                return False
            state["authenticated"] = True
            self.send(writer, self.greeting())
            self.update_client_count()
            return True
        if too_long:
            self.send(writer, b":ERROR line too long\r\n")
            return True
        command = line.split(" ", 1)[0].upper()
        if command not in ("KA", "STATUS", "AUTH"):
            self.forward(line)
        return True

    async def handle_client(self, reader, writer) -> None:
        state = {"authenticated": self.token is None, "pending": b"", "too_long": False}
        if state["authenticated"]:
            if self.authenticated_count() >= MAX_CLIENTS:
                writer.close()
                return
        else:
            self.make_room_for_pending_client()
        peer = writer.get_extra_info("peername")
        self.clients[writer] = state
        logger.info("Client connected: %s (%d total)", peer, len(self.clients))
        if state["authenticated"]:
            self.send(writer, self.greeting())
            self.update_client_count()
        loop = asyncio.get_running_loop()
        auth_deadline = loop.time() + AUTH_TIMEOUT
        try:
            while writer in self.clients:
                timeout = None if state["authenticated"] else max(0, auth_deadline - loop.time())
                data = await asyncio.wait_for(reader.read(1024), timeout)
                if not data:
                    break
                if not all(self.handle_line(writer, state, raw, too_long) for raw, too_long in self.split_lines(state, data)):
                    break
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            self.clients.pop(writer, None)
            writer.close()
            self.update_client_count()
            logger.info("Client disconnected: %s (%d total)", peer, len(self.clients))

    async def run(self) -> None:
        """Shares the detector until ``stop()`` is called."""
        from lightning_bridge.discovery import Advertiser

        self.loop = asyncio.get_running_loop()
        self.stopped = asyncio.Event()
        if self.stopping.is_set():
            return
        detector = threading.Thread(target=self.device_thread, name="detector", daemon=True)
        detector.start()
        self.server = await asyncio.start_server(self.handle_client, self.config.host, self.config.port)
        port = self.server.sockets[0].getsockname()[1]
        logger.info("Sharing on port %d%s", port, "" if self.token else " (no token: anyone on the network can connect)")
        self.update_status(running=True, message="Waiting for the detector")
        tasks = []
        if self.config.advertise:
            advertiser = Advertiser(self.config.name, self.description, port, self.token is not None)
            tasks.append(asyncio.ensure_future(advertiser.run(self.stopped)))
        try:
            await self.stopped.wait()
        finally:
            self.stopping.set()
            self.server.close()
            # Python 3.12+ wait_closed() also waits for open connections, so close them first.
            for writer in list(self.clients):
                writer.close()
            self.clients.clear()
            await self.server.wait_closed()
            for task in tasks:
                await task
            await asyncio.get_running_loop().run_in_executor(None, detector.join, RETRY_SECONDS + 2)
            self.update_status(running=False, detector_connected=False, clients=0, message="Stopped")
            logger.info("Stopped sharing")

    def stop(self) -> None:
        """Safe to call from any thread."""
        self.stopping.set()
        if self.loop is not None and self.stopped is not None:
            self.loop.call_soon_threadsafe(self.stopped.set)


class BackgroundBridge:
    """Runs a Bridge on its own thread and event loop (used by the tray app)."""

    def __init__(self, config: Config, observer=None):
        self.config = config
        self.observer = observer
        self.bridge = None
        self.thread = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.bridge = Bridge(self.config)
        if self.observer:
            self.bridge.add_observer(self.observer)
        self.thread = threading.Thread(target=self._run, name="bridge", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self.bridge.run())
        except OSError as error:
            logger.error("Could not start sharing: %s", error)
            self.bridge.update_status(running=False, message="Could not start: %s" % error)

    def stop(self, timeout: float = RETRY_SECONDS + 5) -> None:
        if self.bridge is not None:
            self.bridge.stop()
        if self.thread is not None:
            self.thread.join(timeout)
        self.thread = None
