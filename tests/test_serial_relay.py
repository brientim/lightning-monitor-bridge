# SPDX-License-Identifier: GPL-3.0-or-later
"""NexStorm's LD-250 mode through a virtual COM port: the relay between a shared detector and a serial port.
A pseudo-terminal stands in for the com0com pair (POSIX only)."""

import os
import select
import sys
import threading
import time
import unittest

from lightning_bridge import devices
from lightning_bridge.config import Config
from lightning_bridge.serial_relay import TextFramer, as_ld250, ld250_squelch, ld350_trigger_level, SerialRelay
from test_nexstorm import LoopbackBridge, capture_struct, eventually, lts3_block, nmea


class ConversionTests(unittest.TestCase):
    def test_squelch_tables_match_boltek(self):
        self.assertEqual([ld350_trigger_level(s) for s in (0, 1, 2, 4, 8, 15)], [40, 43, 47, 53, 58, 78])
        self.assertEqual([ld250_squelch(level) for level in (40, 53, 76, 100, 0)], [0, 4, 14, 15, 0])

    def test_ld350_lines_become_ld250_lines(self):
        self.assertEqual(as_ld250(nmea("$WIMSU,2,17,1,0,0,0").decode()), nmea("$WIMST,2,17,1,0,000.0").decode())
        self.assertEqual(as_ld250(":SQ 53"), ":SQUELCH 4 (0-15)")
        line = nmea("$WIMLI,10,11,045.0").decode()
        self.assertEqual(as_ld250(line), line)

    def test_framer_leaves_out_captures_with_and_without_an_lts3_card(self):
        samples = [index % 4096 for index in range(512)]
        with_card = devices.capture_frame(capture_struct(samples, samples, [1] * 512, lts3_block()))
        without_card = devices.capture_frame(capture_struct(samples, samples, [1] * 512))
        framer = TextFramer()
        stream = with_card + b"$WIMLN*AB\r\n" + without_card + b"$WIMLI,1,2,3.0*00\r\n"
        lines = []
        for start in range(0, len(stream), 700):
            lines += framer.feed(stream[start:start + 700])
        self.assertEqual(lines, [b"$WIMLN*AB", b"$WIMLI,1,2,3.0*00"])


@unittest.skipIf(sys.platform == "win32", "uses a pseudo-terminal in place of a COM port pair")
class RelayTests(unittest.TestCase):
    def start(self, model="LD-350", allow_settings=False, token="secret"):
        self.bridge = LoopbackBridge(token, model)
        self.addCleanup(self.bridge.close)
        self.controller, device = os.openpty()
        self.addCleanup(os.close, self.controller)
        self.addCleanup(os.close, device)
        config = Config(nexstorm_host="127.0.0.1", nexstorm_port=self.bridge.port, nexstorm_token=token,
                        nexstorm_allow_settings=allow_settings, nexstorm_com_port=os.ttyname(device))
        self.relay = SerialRelay(config)
        thread = threading.Thread(target=self.relay.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 8)
        self.addCleanup(self.relay.stop)
        self.assertTrue(eventually(lambda: self.relay.status.detector_connected and self.relay.status.port_open))
        self.received = b""

    def read_until(self, *expected, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not all(item in self.received for item in expected):
            ready, _, _ = select.select([self.controller], [], [], 0.1)
            if ready:
                self.received += os.read(self.controller, 65536)
        return self.received

    def test_an_ld350_reaches_nexstorm_as_an_ld250(self):
        self.start()
        capture = devices.capture_frame(capture_struct([0x800] * 512, [0x7FF] * 512, [128] * 512, lts3_block()))
        self.bridge.send(capture + nmea("$WIMLI,12,13,045.0") + b"\r\n" + nmea("$WIMSU,1,5,0,0,0,0") + b"\r\n:SQ 40\r\n")
        data = self.read_until(b"$WIMST", b":SQUELCH")
        self.assertIn(nmea("$WIMLI,12,13,045.0") + b"\r\n", data)
        self.assertIn(nmea("$WIMST,1,5,0,0,000.0") + b"\r\n", data)
        self.assertIn(b":SQUELCH 0 (0-15)\r\n", data)
        self.assertNotIn(b"[", data)
        self.assertNotIn(b":SERVER", data)

    def test_queries_reach_the_detector_but_settings_only_when_allowed(self):
        self.start()
        os.write(self.controller, b"SQ4\rSQ\rVER\rKA\r")
        self.assertTrue(eventually(lambda: "VER" in self.bridge.device.written))
        self.assertEqual(self.bridge.device.written, ["SQ", "VER"])

    def test_nexstorms_ld250_squelch_becomes_the_ld350_trigger_level(self):
        self.start(allow_settings=True)
        self.bridge.send(nmea("$WIMSU,0,0,0,0,0,0") + b"\r\n")
        self.read_until(b"$WIMST")
        os.write(self.controller, b"SQ4\rSQ 10\rVER\r")
        self.assertTrue(eventually(lambda: "VER" in self.bridge.device.written))
        self.assertEqual(self.bridge.device.written, ["SQ 53", "SQ 64", "VER"])

    def test_an_ld250_is_relayed_as_it_is(self):
        self.start(model="LD-250", allow_settings=True)
        line = nmea("$WIMST,0,3,0,0,123.4") + b"\r\n:SQUELCH 4 (0-15)\r\n"
        self.bridge.send(line)
        self.assertIn(line, self.read_until(b":SQUELCH"))
        os.write(self.controller, b"SQ4\r")
        self.assertTrue(eventually(lambda: self.bridge.device.written == ["SQ4"]))

    def test_a_wrong_token_is_reported(self):
        self.bridge = LoopbackBridge("right", "LD-350")
        self.addCleanup(self.bridge.close)
        controller, device = os.openpty()
        self.addCleanup(os.close, controller)
        self.addCleanup(os.close, device)
        relay = SerialRelay(Config(nexstorm_host="127.0.0.1", nexstorm_port=self.bridge.port, nexstorm_token="wrong",
                                   nexstorm_com_port=os.ttyname(device)))
        thread = threading.Thread(target=relay.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 8)
        self.addCleanup(relay.stop)
        self.assertTrue(eventually(lambda: "check the token" in relay.status.message))


if __name__ == "__main__":
    unittest.main()
