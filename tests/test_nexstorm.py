# SPDX-License-Identifier: GPL-3.0-or-later
"""NexStorm: putting the connector in NexStorm's folder and back, its settings, and the connector itself
(nexstorm/ld350_connector.c, built with this computer's C compiler) reading a bridge on loopback."""

import asyncio
import ctypes
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lightning_bridge import cli, config as settings, devices, discovery, nexstorm, server
from lightning_bridge.config import Config

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "nexstorm" / "ld350_connector.c"
CONNECTOR = b"MZ\x90\x00 Lightning Monitor NexStorm Connector 0.2.0"
BOLTEK = b"MZ\x90\x00 Boltek LD-350 DLL"


def make_nexstorm(folder: Path, subfolders=("x86", "x64")) -> Path:
    """NexStorm as its installer leaves it: NexStorm.exe, and Boltek's DLL in x86 and x64."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "NexStorm.exe").write_bytes(b"MZ")
    for name in subfolders:
        (folder / name).mkdir()
        (folder / name / "LD350_DLL.dll").write_bytes(BOLTEK + name.encode())
    return folder


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.folder = make_nexstorm(self.directory / "NexStorm")
        self.connector = self.directory / "bridge" / "nexstorm" / "LD350_DLL.dll"
        self.connector.parent.mkdir(parents=True)
        self.connector.write_bytes(CONNECTOR)

    def test_install_puts_the_connector_beside_nexstorm_and_keeps_boltek_copies(self):
        nexstorm.install(self.folder, self.connector)
        for dll in (self.folder / "LD350_DLL.dll", self.folder / "x86" / "LD350_DLL.dll", self.folder / "x64" / "LD350_DLL.dll"):
            self.assertEqual(dll.read_bytes(), CONNECTOR)
        self.assertEqual((self.folder / "x86" / "LD350_DLL.dll.boltek").read_bytes(), BOLTEK + b"x86")
        self.assertEqual((self.folder / "x64" / "LD350_DLL.dll.boltek").read_bytes(), BOLTEK + b"x64")
        self.assertFalse((self.folder / "LD350_DLL.dll.boltek").exists())
        self.assertEqual(nexstorm.state(self.folder), "installed")

    def test_installing_again_keeps_boltek_copies(self):
        nexstorm.install(self.folder, self.connector)
        nexstorm.install(self.folder, self.connector)
        self.assertEqual((self.folder / "x86" / "LD350_DLL.dll.boltek").read_bytes(), BOLTEK + b"x86")

    def test_a_dll_from_reinstalling_nexstorm_becomes_the_one_kept(self):
        nexstorm.install(self.folder, self.connector)
        (self.folder / "x86" / "LD350_DLL.dll").write_bytes(BOLTEK + b"newer")
        self.assertEqual(nexstorm.state(self.folder), "partly installed")
        nexstorm.install(self.folder, self.connector)
        self.assertEqual((self.folder / "x86" / "LD350_DLL.dll.boltek").read_bytes(), BOLTEK + b"newer")

    def test_restore_puts_boltek_back_and_removes_the_copy_that_replaced_nothing(self):
        nexstorm.install(self.folder, self.connector)
        nexstorm.restore(self.folder)
        self.assertFalse((self.folder / "LD350_DLL.dll").exists())
        self.assertEqual((self.folder / "x86" / "LD350_DLL.dll").read_bytes(), BOLTEK + b"x86")
        self.assertEqual((self.folder / "x64" / "LD350_DLL.dll").read_bytes(), BOLTEK + b"x64")
        self.assertEqual(sorted(path.name for path in self.folder.rglob("*.boltek")), [])
        self.assertEqual(nexstorm.state(self.folder), "not installed")
        self.assertEqual(nexstorm.restore(self.folder), ["Boltek's driver was already in place"])

    def test_boltek_dll_beside_nexstorm_is_kept_too(self):
        (self.folder / "LD350_DLL.dll").write_bytes(BOLTEK + b"root")
        nexstorm.install(self.folder, self.connector)
        self.assertEqual((self.folder / "LD350_DLL.dll.boltek").read_bytes(), BOLTEK + b"root")
        nexstorm.restore(self.folder)
        self.assertEqual((self.folder / "LD350_DLL.dll").read_bytes(), BOLTEK + b"root")

    def test_install_refuses_a_folder_without_nexstorm_or_a_dll_that_is_not_the_connector(self):
        with self.assertRaisesRegex(nexstorm.NexStormError, "NexStorm.exe"):
            nexstorm.install(self.directory, self.connector)
        with self.assertRaisesRegex(nexstorm.NexStormError, "not the Lightning Monitor NexStorm Connector"):
            nexstorm.install(self.folder, self.folder / "x86" / "LD350_DLL.dll")
        with self.assertRaisesRegex(nexstorm.NexStormError, "missing"):
            nexstorm.install(self.folder, self.directory / "absent.dll")

    def test_a_protected_folder_asks_for_administrator_but_a_running_nexstorm_does_not(self):
        with mock.patch.object(nexstorm.shutil, "copyfile", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaises(nexstorm.NexStormError) as raised:
                nexstorm.install(self.folder, self.connector)
        self.assertTrue(raised.exception.needs_administrator)
        in_use = PermissionError(13, "The process cannot access the file")
        in_use.winerror = 32
        with mock.patch.object(nexstorm.shutil, "copyfile", side_effect=in_use):
            with self.assertRaisesRegex(nexstorm.NexStormError, "quit NexStorm") as raised:
                nexstorm.install(self.folder, self.connector)
        self.assertFalse(raised.exception.needs_administrator)

    def test_finds_nexstorm_in_program_files(self):
        program_files = self.directory / "Program Files (x86)"
        found = make_nexstorm(program_files / "Astrogenic" / "NexStorm", ())
        with mock.patch.object(nexstorm, "_registry_install_locations", return_value=[]), \
                mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(program_files), "ProgramFiles": str(self.directory)}):
            self.assertEqual(nexstorm.find_nexstorm(), found)
            self.assertEqual(nexstorm.find_nexstorm(str(self.folder)), self.folder)


class SettingsFileTests(unittest.TestCase):
    def test_settings_file_has_what_the_connector_reads(self):
        config = Config(nexstorm_host="My-Mac.local", nexstorm_port=10002, nexstorm_token="secret", nexstorm_allow_settings=True)
        lines = [line for line in nexstorm.settings_text(config).splitlines() if not line.startswith(";")]
        self.assertEqual(lines, ["host = My-Mac.local", "port = 10002", "token = secret", "allow_settings = 1", "squelch_scale = auto"])

    def test_hosts_and_tokens_with_spaces_are_refused(self):
        with self.assertRaises(ValueError):
            Config(nexstorm_host="My Mac").validate()
        with self.assertRaises(ValueError):
            Config(nexstorm_port=0).validate()

    def test_connect_command_saves_the_settings_and_writes_the_connectors_file(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        config_path = directory / "config.json"
        ini = directory / "nexstorm-connector.ini"
        with mock.patch.object(nexstorm, "settings_path", return_value=ini), \
                mock.patch.object(nexstorm, "find_nexstorm", return_value=None), \
                mock.patch("sys.stdout"):
            cli.main(["--config", str(config_path), "nexstorm", "connect", "My-Mac.local", "--token", "abc", "--allow-settings"])
        config = settings.load(config_path)
        self.assertEqual((config.nexstorm_host, config.nexstorm_port, config.nexstorm_token, config.nexstorm_allow_settings),
                         ("My-Mac.local", 10001, "abc", True))
        self.assertIn("token = abc", ini.read_text())

    def test_this_pc_reads_the_detector_this_bridge_shares(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        config_path = directory / "config.json"
        settings.save(Config(port=10005, token="bridge-token"), config_path)
        ini = directory / "nexstorm-connector.ini"
        with mock.patch.object(nexstorm, "settings_path", return_value=ini), \
                mock.patch.object(nexstorm, "find_nexstorm", return_value=None), \
                mock.patch("sys.stdout"):
            cli.main(["--config", str(config_path), "nexstorm", "connect", "this-pc"])
        config = settings.load(config_path)
        self.assertEqual((config.nexstorm_host, config.nexstorm_port, config.nexstorm_token), ("127.0.0.1", 10005, "bridge-token"))

    def test_the_bridge_never_opens_the_connector_as_boltek_dll(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        connector = directory / "LD350_DLL.dll"
        connector.write_bytes(CONNECTOR)
        with self.assertRaisesRegex(devices.DetectorError, "NexStorm connector"):
            devices.LD350BoltekDevice(str(connector), loader=lambda _path: self.fail("loaded the connector"))
        with mock.patch.object(devices, "bundle_directory", return_value=directory), \
                mock.patch.object(devices.Path, "cwd", return_value=directory):
            self.assertIsNone(devices.find_boltek_dll())

    def test_shared_detector_records_are_read_like_lightning_monitor(self):
        properties = {b"host": b"My-Mac.local", b"port": b"10001", b"server": b"Lightning Monitor LD-350", b"auth": b"1"}
        detector = discovery.shared_detector("My Mac." + discovery.SERVICE_TYPE, properties, "ignored.local.", 1)
        self.assertEqual(detector, discovery.SharedDetector("My Mac", "My-Mac.local", 10001, "Lightning Monitor LD-350", True))
        self.assertIsNone(discovery.shared_detector("x", {b"port": b"10001"}, None, None))
        self.assertEqual(discovery.shared_detector("x", {}, "pi.local.", 10002).host, "pi.local")


# The connector itself


class Message(ctypes.Structure):
    _fields_ = [("message", ctypes.c_char * 50)]


class Capture(ctypes.Structure):
    _fields_ = [
        ("East", ctypes.c_int * 512), ("North", ctypes.c_int * 512), ("E_Field", ctypes.c_int * 512),
        ("LTS3_Data", ctypes.c_ubyte * 167),
    ]


def build_connector():
    """Builds the connector as a shared library for this computer, or returns None when that isn't possible."""
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if sys.platform == "win32" or compiler is None:
        return None
    output = Path(tempfile.mkdtemp()) / ("libld350_connector" + (".dylib" if sys.platform == "darwin" else ".so"))
    subprocess.run(
        [compiler, "-std=c11", "-D_DEFAULT_SOURCE", "-D_DARWIN_C_SOURCE", "-Wall", "-Werror", "-O1", "-fPIC", "-shared",
         "-o", str(output), str(SOURCE), "-lpthread"],
        check=True, capture_output=True,
    )
    library = ctypes.CDLL(str(output))
    # Read as a whole int, as NexStorm does: "nothing waiting" must be 0 in every byte, not just the lowest.
    for name in ("LD350_MessageReady", "LD350_CaptureReady"):
        getattr(library, name).restype = ctypes.c_uint32
    library.LD350_Command.argtypes = [ctypes.c_char_p]
    library.LD350_SetTriggerLevel.argtypes = [ctypes.c_ubyte]
    library.LD350_GetMessageData.argtypes = [ctypes.POINTER(Message)]
    library.LD350_GetCaptureData.argtypes = [ctypes.POINTER(Capture)]
    return library


def nmea(body: str) -> bytes:
    """A sentence with its NMEA checksum (XOR of the characters between $ and *)."""
    checksum = 0
    for character in body[1:]:
        checksum ^= ord(character)
    return ("%s*%02X" % (body, checksum)).encode()


def capture_struct(east, north, field, lts3=b""):
    return (
        struct.pack("<512i", *east) + struct.pack("<512i", *north) + struct.pack("<512i", *field)
        + lts3.ljust(devices.LTS3_BLOCK_BYTES, b"\0")
    )


def lts3_block():
    block = bytearray(range(devices.LTS3_BLOCK_BYTES))
    block[0] = ord("]")
    block[10:14] = b"@@Ha"
    block[-1] = ord("]")
    return bytes(block)


class FakeDevice:
    line_ending = b"\r\n"

    def __init__(self, model="LD-350"):
        self.model = model
        self.written = []

    def write(self, data):
        self.written.append(data.rstrip(b"\r\n").decode())


class LoopbackBridge:
    """The bridge's sharing server on loopback, on its own thread, with a fake detector."""

    def __init__(self, token="", model="LD-350"):
        config = Config(token=token, host="127.0.0.1", port=0, advertise=False)
        with mock.patch.dict(os.environ, {"LD_PROXY_TOKEN": "", "LIGHTNING_BRIDGE_TOKEN": ""}):
            self.bridge = server.Bridge(config, open_device=lambda: None)
        self.device = FakeDevice(model)
        self.bridge.device = self.device
        self.loop = asyncio.new_event_loop()
        started = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.server = self.loop.run_until_complete(asyncio.start_server(self.bridge.handle_client, "127.0.0.1", 0))
            self.port = self.server.sockets[0].getsockname()[1]
            self.bridge.loop = self.loop
            started.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        started.wait(5)

    def send(self, data: bytes) -> None:
        self.loop.call_soon_threadsafe(self.bridge.broadcast, data)

    def drop_clients(self) -> None:
        def drop():
            for writer in list(self.bridge.clients):
                writer.transport.abort()
        self.loop.call_soon_threadsafe(drop)

    def close(self) -> None:
        async def shut():
            self.server.close()
            for writer in list(self.bridge.clients):
                writer.close()
        asyncio.run_coroutine_threadsafe(shut(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def eventually(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class ConnectorTests(unittest.TestCase):
    library = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.library = build_connector()
        except subprocess.CalledProcessError as error:
            raise AssertionError("The connector did not build:\n%s" % error.stderr.decode()) from None
        if cls.library is None:
            raise unittest.SkipTest("needs a C compiler (the Windows build is made by packaging/build.py)")

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.ini = self.directory / "nexstorm-connector.ini"
        self.log = self.directory / "nexstorm-connector.log"
        environment = mock.patch.dict(os.environ, {"LD350_CONNECTOR_CONFIG": str(self.ini), "LD350_CONNECTOR_LOG": str(self.log)})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(self.library.LD350_Close)

    def start(self, token="", connector_token=None, allow_settings=False, model="LD-350"):
        self.bridge = LoopbackBridge(token, model)
        self.addCleanup(self.bridge.close)
        config = Config(nexstorm_host="127.0.0.1", nexstorm_port=self.bridge.port,
                        nexstorm_token=token if connector_token is None else connector_token,
                        nexstorm_allow_settings=allow_settings)
        nexstorm.write_settings(config, self.ini)
        return self.library.LD350_Open()

    def messages(self):
        found = []
        message = Message()
        while self.library.LD350_MessageReady():
            self.library.LD350_GetMessageData(ctypes.byref(message))
            found.append(message.message.decode())
        return found

    def wait_for_messages(self, count):
        found = []
        eventually(lambda: bool(found.extend(self.messages())) or len(found) >= count)
        return found

    def test_without_a_detector_set_nexstorm_is_told_the_receiver_is_missing(self):
        self.ini.write_text("; nothing yet\n")
        self.assertEqual(self.library.LD350_Open(), 0)
        self.assertIn("No detector set", self.log.read_text())

    def test_a_wrong_token_is_refused_and_never_logged(self):
        self.assertEqual(self.start(token="right-token", connector_token="wrong-token"), 0)
        log = self.log.read_text()
        self.assertIn("check the token", log)
        self.assertNotIn("wrong-token", log)

    def test_detector_lines_reach_nexstorm_with_cr_lf_but_the_greeting_does_not(self):
        self.assertEqual(self.start(token="secret"), 1)
        self.assertEqual(self.library.LD350_KeepAlive(), 1)
        self.bridge.send(b"$WIMLI,12,13,045.0*41\r\n:VER LD350-V1.13\r\n")
        self.assertEqual(self.wait_for_messages(2), ["$WIMLI,12,13,045.0*41\r\n", ":VER LD350-V1.13\r\n"])
        self.assertNotIn("secret", self.log.read_text())

    def test_waveforms_are_unpacked_like_boltek_dll_once_nexstorm_asks_for_them(self):
        east = [(index * 7) & 0xFFF for index in range(512)]
        north = [(index * 13 + 5) & 0xFFF for index in range(512)]
        field = [index & 0xFF for index in range(512)]
        block = lts3_block()
        with_card = devices.capture_frame(capture_struct(east, north, field, block))
        without_card = devices.capture_frame(capture_struct(north, east, field))
        self.assertEqual(self.start(), 1)

        self.bridge.send(with_card + b"$WIMLN*AB\r\n")
        self.assertEqual(self.wait_for_messages(1), ["$WIMLN*AB\r\n"])
        self.assertFalse(self.library.LD350_CaptureReady())

        self.assertEqual(self.library.LD350_RawDataOn(), 1)
        # Without an LTS-3 card the frame ends at "]", and the strike line follows straight after it.
        self.bridge.send(with_card + b"$WIMLI,12,13,045.0*41\r\n" + without_card + b"$WIMLI,40,41,180.0*4A\r\n")
        self.assertEqual(self.wait_for_messages(2), ["$WIMLI,12,13,045.0*41\r\n", "$WIMLI,40,41,180.0*4A\r\n"])
        captures = []
        while self.library.LD350_CaptureReady():
            capture = Capture()
            self.library.LD350_GetCaptureData(ctypes.byref(capture))
            captures.append(capture)
        self.assertEqual(len(captures), 2)
        self.assertEqual((list(captures[0].East), list(captures[0].North), list(captures[0].E_Field)), (east, north, field))
        self.assertEqual(bytes(captures[0].LTS3_Data), block)
        self.assertEqual((list(captures[1].East), list(captures[1].North)), (north, east))
        self.assertEqual(bytes(captures[1].LTS3_Data), bytes(167))

    def test_only_queries_reach_the_detector_unless_nexstorm_may_change_settings(self):
        self.assertEqual(self.start(), 1)
        for command in (b"SQ", b"SQ 50", b"SAVE", b"FT", b"RAW 0", b"KA", b"STATUS", b"VER"):
            self.library.LD350_Command(command)
        self.library.LD350_SetTriggerLevel(60)
        self.library.LD350_ForceTrigger()
        self.library.LD350_Command(b"CAD")
        self.assertTrue(eventually(lambda: "CAD" in self.bridge.device.written))
        self.assertEqual(self.bridge.device.written, ["SQ", "VER", "CAD"])
        self.assertIn(":RAW 0\r\n", self.messages())
        log = self.log.read_text()
        self.assertIn("Not sent, NexStorm may not change the detector's settings: SQ 50\n", log)
        self.assertIn("Not sent, NexStorm may not change the detector's settings: SQ 60\n", log)

    def test_settings_changes_reach_the_detector_when_allowed_but_waveforms_stay_on(self):
        self.assertEqual(self.start(allow_settings=True), 1)
        self.library.LD350_SetTriggerLevel(60)
        self.library.LD350_Command(b"SQ 50")
        self.library.LD350_ForceTrigger()
        self.library.LD350_RawDataOff()
        self.library.LD350_Command(b"RAW 0")
        self.library.LD350_Command(b"SAVE")
        self.assertTrue(eventually(lambda: "SAVE" in self.bridge.device.written))
        self.assertEqual(self.bridge.device.written, ["SQ 60", "SQ 50", "FT", "SAVE"])

    def test_nexstorms_squelch_becomes_the_ld350_trigger_level(self):
        self.assertEqual(self.start(allow_settings=True), 1)
        for squelch in (0, 3, 4, 10, 15):
            self.library.LD350_SetTriggerLevel(squelch)
        self.library.LD350_Command(b"SQ 10")
        self.library.LD350_Command(b"SQ 55")
        self.library.LD350_Command(b"VER")
        self.assertTrue(eventually(lambda: "VER" in self.bridge.device.written))
        # Boltek's LD350.h: squelch 0 = 40, 2 = 47, 4 = 53, 8 = 58, 15 = 78; values between are interpolated.
        self.assertEqual(self.bridge.device.written, ["SQ 40", "SQ 50", "SQ 53", "SQ 64", "SQ 78", "SQ 64", "SQ 55", "VER"])
        self.assertIn("NexStorm squelch 10 is SQ 64 on the LD-350", self.log.read_text())

    def test_a_blocked_squelch_logs_what_it_would_have_been(self):
        self.assertEqual(self.start(), 1)
        self.library.LD350_SetTriggerLevel(10)
        self.library.LD350_Command(b"VER")
        self.assertTrue(eventually(lambda: "VER" in self.bridge.device.written))
        self.assertEqual(self.bridge.device.written, ["VER"])
        self.assertIn("settings: SQ 10 (NexStorm squelch; SQ 64 on the LD-350)", self.log.read_text())

    def test_squelch_scale_ld350_sends_values_unchanged(self):
        self.assertEqual(self.start(allow_settings=True), 1)
        self.ini.write_text(self.ini.read_text() + "squelch_scale = ld350\n")
        self.library.LD350_Close()
        self.assertEqual(self.library.LD350_Open(), 1)
        self.library.LD350_SetTriggerLevel(10)
        self.assertTrue(eventually(lambda: "SQ 10" in self.bridge.device.written))

    def test_reconnects_when_the_connection_drops(self):
        self.assertEqual(self.start(), 1)
        self.bridge.drop_clients()
        self.assertTrue(eventually(lambda: self.library.LD350_KeepAlive() == 0))
        self.assertTrue(eventually(lambda: self.library.LD350_KeepAlive() == 1))
        self.assertTrue(eventually(lambda: self.bridge.bridge.authenticated_count() == 1))
        self.bridge.send(b"$WIMSU,0,0,0,0,0,0*48\r\n")
        self.assertEqual(self.wait_for_messages(1), ["$WIMSU,0,0,0,0,0,0*48\r\n"])
        self.assertIn("reconnecting", self.log.read_text())

    def test_an_ld250_is_presented_to_nexstorm_as_an_ld350(self):
        self.assertEqual(self.start(model="LD-250"), 1)
        self.bridge.send(
            nmea("$WIMST,2,17,1,0,123.4") + b"\r\n"
            + b"$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A\r\n"
            + b":SQUELCH 4 (0-15)\r\n"
            + nmea("$WIMLI,10,11,045.0") + b"\r\n"
        )
        self.assertEqual(self.wait_for_messages(3), [
            nmea("$WIMSU,2,17,1,0,0,0").decode() + "\r\n",
            ":SQ 53\r\n",
            nmea("$WIMLI,10,11,045.0").decode() + "\r\n",
        ])
        self.assertIn("is an LD-250", self.log.read_text())

    def test_nexstorms_squelch_goes_to_an_ld250_on_its_own_scale(self):
        self.assertEqual(self.start(model="LD-250", allow_settings=True), 1)
        for level in (76, 40, 4):
            self.library.LD350_SetTriggerLevel(level)
        self.library.LD350_Command(b"VER")
        self.assertTrue(eventually(lambda: "VER" in self.bridge.device.written))
        # NexStorm's LD-350 mode sends 0-100; the LD-250 takes 0-15 (76 -> 14, 40 -> 0); 0-15 passes unchanged.
        self.assertEqual(self.bridge.device.written, ["SQ 14", "SQ 0", "SQ 4", "VER"])
        self.assertIn("NexStorm squelch 76 is SQ 14 on the LD-250", self.log.read_text())

    def test_a_plain_bridge_with_an_ld250_is_recognised_by_its_status_line(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def serve():
            connection, _ = listener.accept()
            connection.sendall(nmea("$WIMST,0,3,0,0,000.0") + b"\r\n")
            time.sleep(1)
            connection.close()

        threading.Thread(target=serve, daemon=True).start()
        nexstorm.write_settings(Config(nexstorm_host="127.0.0.1", nexstorm_port=listener.getsockname()[1]), self.ini)
        self.assertEqual(self.library.LD350_Open(), 1)
        self.assertEqual(self.wait_for_messages(1), [nmea("$WIMSU,0,3,0,0,0,0").decode() + "\r\n"])

    def test_a_plain_bridge_without_a_greeting_is_used_as_it_is(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def serve():
            connection, _ = listener.accept()
            connection.sendall(b"$WIMSU,1,2,0,0,0,0*48\r\n")
            time.sleep(1)
            connection.close()

        threading.Thread(target=serve, daemon=True).start()
        nexstorm.write_settings(Config(nexstorm_host="127.0.0.1", nexstorm_port=listener.getsockname()[1]), self.ini)
        self.assertEqual(self.library.LD350_Open(), 1)
        self.assertEqual(self.wait_for_messages(1), ["$WIMSU,1,2,0,0,0,0*48\r\n"])


if __name__ == "__main__":
    unittest.main()
