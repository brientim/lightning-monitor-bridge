# SPDX-License-Identifier: GPL-3.0-or-later
"""The bridge's network side with many simultaneous clients (ported from ld_proxy.py's tests).

A fake detector stands in for the LD-350, so no hardware, pyftdi or pyserial is needed.
"""

import asyncio
import unittest
from unittest import mock

from lightning_bridge import server
from lightning_bridge.config import Config

GREETING = b":SERVER Lightning Monitor Bridge LD-350\r\n"


class FakeDevice:
    line_ending = b"\r\n"
    model = "LD-350"

    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    @property
    def lines(self):
        return [data.rstrip(b"\r\n").decode() for data in self.written]


class ProxyTestCase(unittest.IsolatedAsyncioTestCase):
    token = None

    async def asyncSetUp(self):
        config = Config(token=self.token or "", host="127.0.0.1", port=0, advertise=False)
        with mock.patch.dict("os.environ", {"LD_PROXY_TOKEN": "", "LIGHTNING_BRIDGE_TOKEN": ""}):
            self.proxy = server.Bridge(config, open_device=lambda: None)
        self.proxy.loop = asyncio.get_running_loop()
        self.device = FakeDevice()
        self.proxy.device = self.device
        self.server = await asyncio.start_server(self.proxy.handle_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.writers = []

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
        self.server.close()
        await self.server.wait_closed()
        mock.patch.stopall()

    async def connect(self, token=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        if token is not None:
            writer.write(b"AUTH %s\r\n" % token.encode())
            await writer.drain()
        return reader, writer

    async def connect_all(self, count, token=None):
        return await asyncio.gather(*(self.connect(token) for _ in range(count)))

    async def eventually(self, condition, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    def authenticated_count(self):
        return sum(1 for state in self.proxy.clients.values() if state["authenticated"])


class ConcurrentClientTests(ProxyTestCase):
    async def test_every_concurrent_client_receives_the_same_bytes_in_order(self):
        clients = await self.connect_all(8)
        for reader, _ in clients:
            self.assertEqual(await asyncio.wait_for(reader.readexactly(len(GREETING)), 2), GREETING)

        payload = b""
        for index in range(200):
            line = b"$WIMLI,%d,%d,%d.0*00\r\n" % (index, index + 1, index % 360)
            payload += line
            self.proxy.broadcast(line)
            if index % 50 == 0:
                capture = bytes(range(256))
                payload += capture
                self.proxy.broadcast(capture)

        for reader, _ in clients:
            self.assertEqual(await asyncio.wait_for(reader.readexactly(len(payload)), 5), payload)

    async def test_commands_from_concurrent_clients_arrive_whole_and_in_each_clients_order(self):
        client_count, commands_per_client = 6, 25
        clients = await self.connect_all(client_count)
        self.assertTrue(await self.eventually(lambda: len(self.proxy.clients) == client_count))

        async def send_commands(client_index, writer):
            for command_index in range(commands_per_client):
                command = b"SQ %d-%d" % (client_index, command_index)
                # Split every command across two writes so partial lines from different clients interleave.
                writer.write(command[:3])
                await writer.drain()
                await asyncio.sleep(0.001)
                writer.write(command[3:] + b"\r\nKA\r\nSTATUS\r\n")
                await writer.drain()

        await asyncio.gather(*(send_commands(index, writer) for index, (_, writer) in enumerate(clients)))
        expected_total = client_count * commands_per_client
        self.assertTrue(await self.eventually(lambda: len(self.device.written) >= expected_total))
        lines = self.device.lines
        self.assertEqual(len(lines), expected_total, "KA and STATUS are dropped")
        for client_index in range(client_count):
            from_client = [line for line in lines if line.startswith("SQ %d-" % client_index)]
            self.assertEqual(from_client, ["SQ %d-%d" % (client_index, n) for n in range(commands_per_client)])

    async def test_disconnecting_some_clients_does_not_disturb_the_others(self):
        clients = await self.connect_all(5)
        self.assertTrue(await self.eventually(lambda: len(self.proxy.clients) == 5))
        for reader, _ in clients:
            await asyncio.wait_for(reader.readexactly(len(GREETING)), 2)

        for _, writer in clients[:2]:
            writer.close()
            await writer.wait_closed()
        self.assertTrue(await self.eventually(lambda: len(self.proxy.clients) == 3))

        self.proxy.broadcast(b"$WIMST,1,2,3\r\n")
        for reader, _ in clients[2:]:
            self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b"$WIMST,1,2,3\r\n")

    async def test_connections_beyond_the_maximum_are_refused_until_a_slot_frees(self):
        clients = await self.connect_all(server.MAX_CLIENTS)
        self.assertTrue(await self.eventually(lambda: len(self.proxy.clients) == server.MAX_CLIENTS))

        extra_reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(extra_reader.read(), 2), b"", "The proxy closes a connection over the limit")
        self.assertEqual(len(self.proxy.clients), server.MAX_CLIENTS)

        clients[0][1].close()
        self.assertTrue(await self.eventually(lambda: len(self.proxy.clients) == server.MAX_CLIENTS - 1))
        replacement_reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(replacement_reader.readline(), 2), GREETING)

    async def test_stalled_client_is_disconnected_without_holding_up_others(self):
        with mock.patch.object(server, "MAX_QUEUED_BYTES", 256 * 1024):
            healthy_reader, _ = await self.connect()
            await self.connect()  # never read from: its reader stops pulling once its small buffer fills
            self.assertTrue(await self.eventually(lambda: self.authenticated_count() == 2))

            received = bytearray()

            async def read_all():
                while chunk := await healthy_reader.read(65536):
                    received.extend(chunk)

            reading = asyncio.create_task(read_all())
            chunk = b"A" * 8192
            chunks_sent = 0
            while self.authenticated_count() == 2 and chunks_sent < 2000:
                self.proxy.broadcast(chunk)
                chunks_sent += 1
                await asyncio.sleep(0.002)
            self.assertEqual(self.authenticated_count(), 1, "The stalled client is dropped")

            expected = len(GREETING) + chunks_sent * len(chunk)
            self.assertTrue(await self.eventually(lambda: len(received) >= expected))
            self.assertEqual(len(received), expected)
            reading.cancel()

    async def test_overlong_command_lines_are_rejected_not_cut_short(self):
        reader, writer = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.readline(), 2), GREETING)

        longest = b"SQ " + b"1" * (server.MAX_LINE - 3)
        too_long = longest + b"2"
        writer.write(too_long[:100])
        await writer.drain()
        await asyncio.sleep(0.01)
        writer.write(too_long[100:] + b"\r\n" + longest + b"\r\nVER\r\n")
        await writer.drain()

        self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b":ERROR line too long\r\n")
        self.assertTrue(await self.eventually(lambda: len(self.device.written) == 2))
        self.assertEqual(self.device.lines, [longest.decode(), "VER"])
        self.assertEqual(len(self.proxy.clients), 1)


class AuthenticationTests(ProxyTestCase):
    token = "s3cret"

    async def test_concurrent_clients_with_right_and_wrong_tokens(self):
        good, bad = await asyncio.gather(self.connect_all(5, "s3cret"), self.connect_all(5, "guess"))
        for reader, _ in good:
            self.assertEqual(await asyncio.wait_for(reader.readline(), 2), GREETING)
        for reader, _ in bad:
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b":ERROR unauthorised\r\n")
        self.assertTrue(await self.eventually(lambda: len(self.proxy.clients) == 5))

        self.proxy.broadcast(b"$WIMLI,1,2,3\r\n")
        for reader, _ in good:
            self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b"$WIMLI,1,2,3\r\n")
        self.assertEqual(self.device.written, [])

    async def test_idle_unauthenticated_connections_cannot_lock_out_real_clients(self):
        idle = await self.connect_all(server.MAX_CLIENTS + 4)
        self.assertTrue(await self.eventually(lambda: len(self.proxy.clients) == server.MAX_PENDING_CLIENTS))

        member_reader, _ = await self.connect("s3cret")
        self.assertEqual(await asyncio.wait_for(member_reader.readline(), 2), GREETING)
        self.assertEqual(self.authenticated_count(), 1)
        dropped = [await asyncio.wait_for(reader.read(), 2) for reader, _ in idle[:5]]
        self.assertEqual(dropped, [b""] * 5, "The longest-waiting connections were dropped without data")

    async def test_authenticated_clients_beyond_the_maximum_are_turned_away(self):
        members = await self.connect_all(server.MAX_CLIENTS, "s3cret")
        for reader, _ in members:
            self.assertEqual(await asyncio.wait_for(reader.readline(), 2), GREETING)

        extra_reader, _ = await self.connect("s3cret")
        self.assertEqual(await asyncio.wait_for(extra_reader.read(), 2), b":ERROR too many clients\r\n")
        self.assertEqual(self.authenticated_count(), server.MAX_CLIENTS)

    async def test_overlong_auth_line_is_unauthorised_even_if_it_starts_with_the_token(self):
        reader, writer = await self.connect()
        writer.write(b"AUTH s3cret" + b" " * server.MAX_LINE + b"x\r\n")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b":ERROR unauthorised\r\n")
        self.assertEqual(self.authenticated_count(), 0)

    async def test_trickling_bytes_does_not_extend_the_authentication_timeout(self):
        with mock.patch.object(server, "AUTH_TIMEOUT", 0.3):
            reader, writer = await self.connect()
            for _ in range(10):
                if writer.is_closing() or reader.at_eof():
                    break
                writer.write(b"A")
                await asyncio.sleep(0.1)
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            self.assertEqual(len(self.proxy.clients), 0)

    async def test_client_that_never_authenticates_gets_nothing_and_is_dropped_after_the_timeout(self):
        with mock.patch.object(server, "AUTH_TIMEOUT", 0.3):
            member_reader, member_writer = await self.connect("s3cret")
            lurker_reader, _ = await self.connect()
            self.assertEqual(await asyncio.wait_for(member_reader.readline(), 2), GREETING)
            self.assertTrue(await self.eventually(lambda: len(self.proxy.clients) == 2))
            self.assertEqual(self.authenticated_count(), 1)

            self.proxy.broadcast(b"$WIMLI,1,2,3\r\n")
            self.assertEqual(await asyncio.wait_for(member_reader.readline(), 2), b"$WIMLI,1,2,3\r\n")
            self.assertEqual(await asyncio.wait_for(lurker_reader.read(), 2), b"", "Dropped without any detector data")
            self.assertTrue(await self.eventually(lambda: len(self.proxy.clients) == 1))
            self.assertFalse(member_writer.is_closing())


if __name__ == "__main__":
    unittest.main()
