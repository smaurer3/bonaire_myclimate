"""Tests for BonairePyClimate.discovery (no Home Assistant needed).

Run from the repository root:  python3 -m unittest discover -s tests -v
Uses 127.0.0.x loopback addresses, so run it on Linux/macOS.
"""
import asyncio
import ipaddress
import os
import re
import socket
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "bonaire_myclimate"))

from BonairePyClimate import discovery  # noqa: E402

UDP_PORT = 21001
TCP_PORT = 21003
RESPONSE = ("<myclimate><response>discovery</response><ip>{ip}</ip><serial>502-0173-800043</serial>"
            "<version>1.2.3</version><port>10002</port><status>router</status></myclimate>")


class FakeUnit:
    """Behaves like the Wi-Fi module: ignores broadcasts, answers unicast by connecting back."""

    def __init__(self, ip, answer_broadcast=False):
        self.ip = ip
        self.answer_broadcast = answer_broadcast
        self.received_delete = asyncio.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind(("0.0.0.0" if answer_broadcast else ip, UDP_PORT))
        self._sock.setblocking(False)
        self._task = None

    async def start(self):
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        loop = asyncio.get_running_loop()
        while True:
            data, addr = await loop.sock_recvfrom(self._sock, 4096)
            m = re.search(rb"<ip>([^<]*)</ip>", data)
            if not m:
                continue
            asyncio.get_running_loop().create_task(self._connect_back(m.group(1).decode()))

    async def _connect_back(self, ip):
        reader, writer = await asyncio.open_connection(ip, TCP_PORT, local_addr=(self.ip, 0))
        writer.write(RESPONSE.format(ip=self.ip).encode())
        await writer.drain()
        # Like the real module: keep the connection open until told to delete it
        while True:
            data = await reader.read(4096)
            if not data:
                break
            if b"delete" in data:
                self.received_delete.set()
                break
        writer.close()

    async def stop(self):
        self._task.cancel()
        self._sock.close()


def scan(**kw):
    kw.setdefault("port_discovery", UDP_PORT)
    kw.setdefault("port_local", TCP_PORT)
    return discovery.async_scan(**kw)


class ParseTests(unittest.TestCase):
    def test_parses_response(self):
        info = discovery.parse_discovery_response(RESPONSE.format(ip="192.168.1.108"))
        self.assertEqual(info, discovery.UnitInfo("192.168.1.108", "502-0173-800043", "1.2.3"))

    def test_peer_address_wins(self):
        info = discovery.parse_discovery_response(RESPONSE.format(ip="10.0.0.1"), "192.168.1.108")
        self.assertEqual(info.host, "192.168.1.108")

    def test_rejects_other_messages(self):
        self.assertIsNone(discovery.parse_discovery_response("<myclimate><response>installation</response></myclimate>"))
        self.assertIsNone(discovery.parse_discovery_response("not xml"))

    def test_valid_ipv4(self):
        self.assertTrue(discovery.is_valid_ipv4("192.168.1.108"))
        for bad in ("", "192.168.1", "192.168.1.256", "host.local", "::1"):
            self.assertFalse(discovery.is_valid_ipv4(bad), bad)

    def test_normalise_network_caps_size(self):
        self.assertEqual(str(discovery.normalise_network("192.168.1.173", 24)), "192.168.1.0/24")
        self.assertEqual(str(discovery.normalise_network("10.1.2.3", 8)), "10.1.2.0/24")
        self.assertEqual(str(discovery.normalise_network("192.168.4.9", 22)), "192.168.4.0/22")


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.units = []

    async def asyncTearDown(self):
        for u in self.units:
            await u.stop()

    async def add_unit(self, ip, **kw):
        unit = FakeUnit(ip, **kw)
        await unit.start()
        self.units.append(unit)
        return unit

    async def test_unicast_to_known_host_finds_unit_that_ignores_broadcast(self):
        unit = await self.add_unit("127.0.0.2")
        info = await scan(targets=[("127.0.0.1", ipaddress.ip_network("127.0.0.0/29"))],
                          hosts=["127.0.0.2"], sweep=False, timeout=5)
        self.assertIsNotNone(info)
        self.assertEqual((info.host, info.serial), ("127.0.0.2", "502-0173-800043"))
        await asyncio.wait_for(unit.received_delete.wait(), 3)  # scanner releases the unit

    async def test_broadcast_alone_misses_a_unit_that_ignores_it(self):
        await self.add_unit("127.0.0.2")
        info = await scan(targets=[("127.0.0.1", ipaddress.ip_network("127.0.0.0/29"))],
                          hosts=[], sweep=False, timeout=1.5)
        self.assertIsNone(info)

    async def test_sweep_finds_unit_with_unknown_ip(self):
        await self.add_unit("127.0.0.5")
        info = await scan(targets=[("127.0.0.1", ipaddress.ip_network("127.0.0.0/29"))],
                          hosts=[], sweep=True, timeout=8, quick_wait=1)
        self.assertIsNotNone(info)
        self.assertEqual(info.host, "127.0.0.5")

    async def test_sweep_finds_unit_when_known_host_is_stale(self):
        await self.add_unit("127.0.0.6")
        info = await scan(targets=[("127.0.0.1", ipaddress.ip_network("127.0.0.0/29"))],
                          hosts=["127.0.0.3"], sweep=True, timeout=8, quick_wait=1)
        self.assertEqual(info.host, "127.0.0.6")

    async def test_returns_none_when_nothing_answers(self):
        info = await scan(targets=[("127.0.0.1", ipaddress.ip_network("127.0.0.0/29"))],
                          hosts=[], sweep=True, timeout=3, quick_wait=1)
        self.assertIsNone(info)

    async def test_port_busy_raises_oserror(self):
        blocker = await asyncio.start_server(lambda r, w: None, "0.0.0.0", TCP_PORT)
        try:
            with self.assertRaises(OSError):
                await scan(targets=[("127.0.0.1", None)], sweep=False, timeout=1)
        finally:
            blocker.close()
            await blocker.wait_closed()


if __name__ == "__main__":
    unittest.main()
