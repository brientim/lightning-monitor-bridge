# SPDX-License-Identifier: GPL-3.0-or-later
"""Boltek driver backend, detector choice, discovery, settings, service files, command line and the
bridge's start / stop with a fake detector."""

import asyncio
import ctypes
import json
import plistlib
import queue
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lightning_bridge import cli, config as settings, devices, discovery, server, service


class FakeFunction:
    def __init__(self, implementation):
        self.implementation = implementation
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self.implementation(*args)


def capture_struct(east=0, north=0, field=0, lts3=b""):
    """An LD350_CaptureDataType as the DLL fills it: three int[512] arrays, then the LTS-3 block."""
    samples = devices.CAPTURE_SAMPLES
    return (
        struct.pack("<%di" % samples, *[east] * samples)
        + struct.pack("<%di" % samples, *[north] * samples)
        + struct.pack("<%di" % samples, *[field] * samples)
        + lts3.ljust(devices.LTS3_BLOCK_BYTES, b"\0")
    )


def lts3_block():
    """Centiseconds, counter, frequency, checksum, then the GPS receiver's @@Ha report, serial and "]"."""
    block = bytearray(devices.LTS3_BLOCK_BYTES)
    block[10:14] = b"@@Ha"
    block[-1] = ord("]")
    return bytes(block)


class FakeBoltekDLL:
    """Stands in for LD350_DLL.dll: queued messages and captures, recorded commands."""

    def __init__(self, open_result=1):
        self.messages = [b"$WIMLI,12,13,045.0*41", b":VER LD350-V1.13"]
        self.captures = [capture_struct(east=0x800, north=0x7FF, field=128)]
        self.commands = []
        self.keep_alives = 0
        self.raw_data = False
        self.closed = False
        self.open_result = open_result

    def __getitem__(self, name):
        key = {value: key for key, value in devices.LD350BoltekDevice.names.items()}[name]
        return FakeFunction(getattr(self, key))

    def open(self):
        return self.open_result

    def close(self):
        self.closed = True
        return 1

    def keep_alive(self):
        self.keep_alives += 1
        return 1

    def command(self, text):
        self.commands.append(text)
        return 1

    def message_ready(self):
        return bool(self.messages)

    def get_message(self, buffer):
        buffer.value = self.messages.pop(0)

    def raw_data_on(self):
        self.raw_data = True
        return 1

    def capture_ready(self):
        return bool(self.captures)

    def get_capture(self, buffer):
        capture = self.captures.pop(0)
        ctypes.memmove(buffer, capture, len(capture))


class BoltekBackendTests(unittest.TestCase):
    def make(self, dll):
        return devices.LD350BoltekDevice("LD350_DLL.dll", loader=lambda _path: dll)

    def test_turns_on_waveforms_when_opened(self):
        dll = FakeBoltekDLL()
        device = self.make(dll)
        self.assertTrue(dll.raw_data)
        self.assertTrue(device.waveforms)

    def test_sends_each_capture_as_the_detectors_frame_before_the_lines(self):
        dll = FakeBoltekDLL()
        device = self.make(dll)
        data = device.read()
        frame = devices.capture_frame(capture_struct(east=0x800, north=0x7FF, field=128))
        self.assertTrue(data.startswith(frame))
        self.assertEqual(data[len(frame):], b"$WIMLI,12,13,045.0*41\r\n:VER LD350-V1.13\r\n")
        self.assertEqual(device.read(), b"")

    def test_capture_frame_packs_four_bytes_per_sample_like_the_ld350(self):
        frame = devices.capture_frame(capture_struct(east=0xABC, north=0x123, field=0x45))
        self.assertEqual(frame[:5], b"[\x1a\xbc\x45\x23")
        self.assertEqual(len(frame), 1 + devices.CAPTURE_SAMPLES * 4 + 1)
        self.assertEqual(frame[-1:], b"]")

    def test_capture_frame_keeps_the_lts3_block_only_when_it_has_a_gps_report(self):
        block = lts3_block()
        with_card = devices.capture_frame(capture_struct(lts3=block))
        self.assertEqual(len(with_card), 1 + devices.CAPTURE_SAMPLES * 4 + devices.LTS3_BLOCK_BYTES)
        self.assertEqual(with_card[-devices.LTS3_BLOCK_BYTES:], block)
        without_card = devices.capture_frame(capture_struct())
        self.assertEqual(without_card[-1:], b"]")
        self.assertEqual(len(without_card), 1 + devices.CAPTURE_SAMPLES * 4 + 1)

    def test_keep_alive_uses_the_dll_call_and_other_lines_are_commands(self):
        dll = FakeBoltekDLL()
        device = self.make(dll)
        opened_keep_alives = dll.keep_alives
        device.write(b"KA\r\n")
        device.write(b"SQ 49\r\nVER\r\n")
        self.assertEqual(dll.keep_alives, opened_keep_alives + 1)
        self.assertEqual(dll.commands, [b"SQ 49", b"VER"])
        self.assertEqual(device.startup_commands(), [b"VER"])
        device.close()
        self.assertTrue(dll.closed)

    def test_open_failure_explains_nexstorm(self):
        with self.assertRaises(devices.DetectorError) as raised:
            self.make(FakeBoltekDLL(open_result=0))
        self.assertIn("NexStorm", str(raised.exception))

    @unittest.skipIf(sys.platform == "win32", "checks the non-Windows message")
    def test_boltek_driver_is_windows_only(self):
        with self.assertRaises(devices.DetectorError):
            devices.LD350BoltekDevice()


class DetectorChoiceTests(unittest.TestCase):
    def test_ld250_needs_a_serial_port(self):
        with self.assertRaises(devices.DetectorError):
            devices.open_device(settings.Config(detector="ld250"))

    def test_windows_auto_falls_back_to_boltek_driver(self):
        boltek = object()
        with mock.patch.object(devices.sys, "platform", "win32"), \
                mock.patch.object(devices, "LD350Device", side_effect=OSError("no WinUSB driver")), \
                mock.patch.object(devices, "LD350BoltekDevice", return_value=boltek):
            self.assertIs(devices.open_device(settings.Config()), boltek)

    def test_windows_auto_reports_both_failures(self):
        with mock.patch.object(devices.sys, "platform", "win32"), \
                mock.patch.object(devices, "LD350Device", side_effect=OSError("no WinUSB driver")), \
                mock.patch.object(devices, "LD350BoltekDevice", side_effect=devices.DetectorError("no DLL")):
            with self.assertRaises(devices.DetectorError) as raised:
                devices.open_device(settings.Config())
        self.assertIn("WinUSB: no WinUSB driver", str(raised.exception))
        self.assertIn("Boltek driver: no DLL", str(raised.exception))

    def test_explicit_boltek_driver(self):
        boltek = object()
        with mock.patch.object(devices, "LD350BoltekDevice", return_value=boltek) as made:
            self.assertIs(devices.open_device(settings.Config(ld350_driver="boltek", boltek_dll="C:/x/LD350_DLL.dll")), boltek)
        made.assert_called_once_with("C:/x/LD350_DLL.dll")


class DiscoveryTests(unittest.TestCase):
    def test_txt_record_matches_lightning_monitor(self):
        self.assertEqual(
            discovery.txt_properties("Lightning Monitor Bridge LD-350", "weather-pc-bridge.local", 10001, True),
            {"server": "Lightning Monitor Bridge LD-350", "host": "weather-pc-bridge.local", "port": "10001", "auth": "1"},
        )
        self.assertEqual(discovery.SERVICE_TYPE, "_lightningmon._tcp.local.")

    def test_host_name_is_a_safe_distinct_mdns_name(self):
        self.assertEqual(discovery.mdns_host_name("Weather PC_2"), "weather-pc-2-bridge.local")
        self.assertEqual(discovery.mdns_host_name("---"), "lightning-bridge.local")

    def test_service_info_carries_the_txt_record_and_addresses(self):
        info = discovery.service_info("Weather PC", "Lightning Monitor Bridge LD-250", 10002, False, ["192.0.2.20"])
        self.assertEqual(info.type, discovery.SERVICE_TYPE)
        self.assertEqual(info.port, 10002)
        self.assertEqual(info.properties[b"auth"], b"0")
        self.assertEqual(info.parsed_addresses(), ["192.0.2.20"])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "config.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_round_trip_and_unknown_keys_are_ignored(self):
        config = settings.Config(detector="ld250", serial_port="COM3", port=10002, token="abc")
        settings.save(config, self.path)
        data = json.loads(self.path.read_text())
        data["future_setting"] = True
        self.path.write_text(json.dumps(data))
        self.assertEqual(settings.load(self.path), config)

    def test_set_converts_types_and_validates(self):
        config = settings.Config()
        config.set("port", "10005")
        config.set("advertise", "no")
        self.assertEqual((config.port, config.advertise), (10005, False))
        with self.assertRaises(ValueError):
            config.set("detector", "ld999")
        with self.assertRaises(KeyError):
            config.set("colour", "red")

    def test_token_from_settings_then_environment(self):
        with mock.patch.dict("os.environ", {"LIGHTNING_BRIDGE_TOKEN": "", "LD_PROXY_TOKEN": "old"}):
            self.assertEqual(settings.Config().effective_token, "old")
            self.assertEqual(settings.Config(token="mine").effective_token, "mine")
        with mock.patch.dict("os.environ", {"LIGHTNING_BRIDGE_TOKEN": "", "LD_PROXY_TOKEN": ""}):
            self.assertIsNone(settings.Config().effective_token)

    def test_new_tokens_are_long_and_different(self):
        first, second = settings.new_token(), settings.new_token()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 32)


class ServiceFileTests(unittest.TestCase):
    arguments = ["/opt/lightning bridge/lightning-bridge", "run", "--config", "/home/pi/.config/lightning-monitor-bridge/config.json"]

    def test_launchd_plist_is_valid_and_keeps_the_bridge_running(self):
        plist = plistlib.loads(service.launchd_plist(self.arguments, Path("/tmp/bridge.log"), user="tim").encode())
        self.assertEqual(plist["Label"], service.LABEL)
        self.assertEqual(plist["ProgramArguments"], self.arguments)
        self.assertTrue(plist["RunAtLoad"] and plist["KeepAlive"])
        self.assertEqual(plist["UserName"], "tim")

    def test_systemd_unit_runs_as_the_user_and_quotes_paths_with_spaces(self):
        unit = service.systemd_unit(self.arguments, "pi")
        self.assertIn("User=pi\n", unit)
        self.assertIn('ExecStart="/opt/lightning bridge/lightning-bridge" run --config /home/pi/.config/lightning-monitor-bridge/config.json\n', unit)
        self.assertIn("Restart=always", unit)

    def test_windows_task_at_logon_or_startup(self):
        logon = service.windows_task_command([r"C:\Program Files\Bridge\lightning-bridge.exe", "run"], False)
        self.assertEqual(logon[logon.index("/TR") + 1], '"C:\\Program Files\\Bridge\\lightning-bridge.exe" run')
        self.assertIn("ONLOGON", logon)
        startup = service.windows_task_command(["bridge.exe", "run"], True)
        self.assertEqual(startup[startup.index("/SC") + 1:startup.index("/SC") + 4], ["ONSTART", "/RU", "SYSTEM"])

    def test_udev_rule_matches_the_ld350(self):
        self.assertIn('ATTR{idVendor}=="0403", ATTR{idProduct}=="f241"', service.UDEV_RULE)


class CommandLineTests(unittest.TestCase):
    def test_old_ld_proxy_options_still_work(self):
        self.assertEqual(cli.normalise(["--ld250", "COM3", "--port", "10002"]), ["run", "--ld250", "COM3", "--port", "10002"])
        self.assertEqual(cli.normalise(["--config", "x.json", "--ld350"]), ["--config", "x.json", "run", "--ld350"])
        self.assertEqual(cli.normalise(["devices"]), ["devices"])

    def test_run_options_override_settings(self):
        options = cli.build_parser().parse_args(["run", "--ld250", "/dev/ttyUSB0", "--port", "10003", "--no-advertise", "--token", "t"])
        config = cli.apply_run_options(settings.Config(), options)
        self.assertEqual((config.detector, config.serial_port, config.port, config.advertise, config.token),
                         ("ld250", "/dev/ttyUSB0", 10003, False, "t"))


class FakeDetector:
    model = "LD-250"
    driver = "fake"
    line_ending = b"\r"

    def __init__(self):
        self.lines = queue.Queue()
        self.written = []
        self.closed = False

    def read(self):
        try:
            return self.lines.get(timeout=0.05)
        except queue.Empty:
            return b""

    def write(self, data):
        self.written.append(data)

    def close(self):
        self.closed = True

    def keep_alive_commands(self, tick):
        return []

    def startup_commands(self):
        return [b"HELLO"]


class BridgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_relays_the_detector_reports_status_and_stops_cleanly(self):
        detector = FakeDetector()
        statuses = []
        config = settings.Config(detector="ld250", host="127.0.0.1", port=0, advertise=False)
        with mock.patch.dict("os.environ", {"LD_PROXY_TOKEN": "", "LIGHTNING_BRIDGE_TOKEN": ""}):
            bridge = server.Bridge(config, open_device=lambda: detector)
        bridge.add_observer(statuses.append)
        running = asyncio.ensure_future(bridge.run())
        while bridge.server is None or not bridge.status.detector_connected:
            await asyncio.sleep(0.01)
        port = bridge.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b":SERVER Lightning Monitor Bridge LD-250\r\n")
        detector.lines.put(b"$WIMST,1,2,3\r\n")
        self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b"$WIMST,1,2,3\r\n")
        self.assertEqual(detector.written[0], b"HELLO\r")
        self.assertEqual(bridge.status.clients, 1)

        bridge.stop()
        await asyncio.wait_for(running, 10)
        self.assertTrue(detector.closed)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertFalse(bridge.status.running)
        self.assertTrue(any(status.detector_connected for status in statuses))
        writer.close()

    def test_background_bridge_stops_even_if_asked_straight_away(self):
        config = settings.Config(detector="ld250", serial_port="/dev/does-not-exist", host="127.0.0.1", port=0, advertise=False)
        runner = server.BackgroundBridge(config)
        start = time.monotonic()
        runner.start()
        runner.stop()
        self.assertFalse(runner.running)
        self.assertLess(time.monotonic() - start, 5)


if __name__ == "__main__":
    unittest.main()
