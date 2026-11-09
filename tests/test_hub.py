"""Tests for the discovery loop in BonairePyClimate.hub, with Home Assistant stubbed out.

Uses the real ports 10001/10003 on 127.0.0.x, so run on Linux/macOS with those ports free.
"""
import asyncio
import ipaddress
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "bonaire_myclimate"))

import test_discovery  # noqa: E402  (provides FakeUnit)


def _stub_homeassistant():
    for name in ("homeassistant", "homeassistant.components", "homeassistant.components.climate",
                 "homeassistant.components.climate.const"):
        sys.modules.setdefault(name, types.ModuleType(name))
    const = sys.modules["homeassistant.components.climate.const"]
    const.ClimateEntityFeature = types.SimpleNamespace(
        TARGET_TEMPERATURE=1, PRESET_MODE=2, FAN_MODE=4, TURN_OFF=8, TURN_ON=16)
    const.HVACMode = types.SimpleNamespace(OFF="off", FAN_ONLY="fan_only", HEAT="heat", COOL="cool")


_stub_homeassistant()
from BonairePyClimate import hub as hub_module  # noqa: E402


class HubDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_ports = (test_discovery.UDP_PORT, test_discovery.TCP_PORT)
        test_discovery.UDP_PORT, test_discovery.TCP_PORT = 10001, 10003
        self.unit = None
        self.hub = None
        self.found = []

    async def asyncTearDown(self):
        if self.hub is not None:
            await self.hub.async_teardown()
        if self.unit is not None:
            await self.unit.stop()
        test_discovery.UDP_PORT, test_discovery.TCP_PORT = self._old_ports

    async def start(self, unit_ip, **hub_kwargs):
        self.unit = test_discovery.FakeUnit(unit_ip)
        await self.unit.start()
        hass = types.SimpleNamespace(loop=asyncio.get_running_loop())
        self.hub = hub_module.Hub(hass, "127.0.0.1", on_host_found=self.found.append, **hub_kwargs)

    async def wait_connected(self, timeout):
        for _ in range(int(timeout * 10)):
            if self.hub._connected:
                return True
            await asyncio.sleep(0.1)
        return False

    async def test_known_host_connects_on_first_attempt(self):
        await self.start("127.0.0.2", host="127.0.0.2")
        self.assertTrue(await self.wait_connected(5))
        self.assertEqual(self.found, ["127.0.0.2"])

    async def test_last_host_is_used_when_no_host_configured(self):
        await self.start("127.0.0.3", last_host="127.0.0.3")
        self.assertTrue(await self.wait_connected(5))

    async def test_unit_that_moved_is_found_by_sweep_and_reported(self):
        # stale last_host; the unit is really on .5; the subnet sweep happens on the 3rd attempt (~20 s)
        await self.start("127.0.0.5", last_host="127.0.0.2",
                         network=ipaddress.ip_network("127.0.0.0/29"))
        self.assertFalse(await self.wait_connected(5))      # not found by the quick attempts
        self.assertTrue(await self.wait_connected(25))      # found once the sweep runs
        self.assertEqual(self.found, ["127.0.0.5"])         # new address reported for saving
        self.assertEqual(self.hub._known_hosts(), ["127.0.0.5"])

    async def test_known_hosts_order_and_dedupe(self):
        hass = types.SimpleNamespace(loop=asyncio.get_running_loop())
        self.hub = hub_module.Hub(hass, "127.0.0.1", host="127.0.0.2", last_host="127.0.0.2")
        self.assertEqual(self.hub._known_hosts(), ["127.0.0.2"])
        self.hub._last_host = "127.0.0.9"
        self.assertEqual(self.hub._known_hosts(), ["127.0.0.2", "127.0.0.9"])
        self.hub.configured_host = None
        self.assertEqual(self.hub._known_hosts(), ["127.0.0.9"])


if __name__ == "__main__":
    unittest.main()
