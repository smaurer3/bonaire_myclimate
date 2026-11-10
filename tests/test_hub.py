"""Tests for the discovery loop in BonairePyClimate.hub, with Home Assistant stubbed out.

Uses the real ports 10001/10003 on 127.0.0.x, so run on Linux/macOS with those ports free.
"""
import asyncio
import ipaddress
import os
import sys
import types
import unittest
import xml.etree.ElementTree as ET

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


class ProtocolUnit(test_discovery.FakeUnit):
    """A module that speaks the whole protocol, and can be told to misbehave with commands."""

    def __init__(self, ip):
        super().__init__(ip)
        self.state = dict(system="off", type="evap", mode="thermo", setPoint="5",
                          fanSpeed="3", roomTemp="21", zoneList="1")
        self.posts = []        # every postzoneinfo received, as a dict
        self.drop_posts = 0    # ignore this many posts (no acknowledgement at all)
        self.ignore_posts = 0  # acknowledge this many posts but do not apply them

    def _xml(self, **fields):
        return "<myclimate>" + "".join(f"<{k}>{v}</{k}>" for k, v in fields.items()) + "</myclimate>"

    async def _connect_back(self, ip):
        reader, writer = await asyncio.open_connection(ip, test_discovery.TCP_PORT, local_addr=(self.ip, 0))
        writer.write(test_discovery.RESPONSE.format(ip=self.ip).encode())
        await writer.drain()
        buffer = ""
        while True:
            data = await reader.read(4096)
            if not data:
                break
            buffer += data.decode()
            while "</myclimate>" in buffer:
                message, buffer = buffer.split("</myclimate>", 1)
                message += "</myclimate>"
                if "delete" in message:
                    self.received_delete.set()
                    writer.close()
                    return
                reply = self._reply(message)
                if reply:
                    writer.write(reply.encode())
                    await writer.drain()
        writer.close()

    def _reply(self, message):
        root = ET.fromstring(message)
        if root.findtext("get") == "installation":
            return ("<myclimate><response>installation</response>"
                    "<appliance><type>heat</type><zoneList/></appliance>"
                    "<appliance><type>cool</type><zoneList/></appliance>"
                    "<appliance><type>evap</type><zoneList>1</zoneList></appliance></myclimate>")
        if root.findtext("get") == "getzoneinfo":
            return self._xml(response="getzoneinfo", **self.state)
        if root.findtext("post") == "postzoneinfo":
            post = {c.tag: c.text for c in root if c.tag != "post"}
            self.posts.append(post)
            if self.drop_posts > 0:
                self.drop_posts -= 1
                return None
            if self.ignore_posts > 0:
                self.ignore_posts -= 1
            else:
                self.state.update(post)
            return self._xml(response="postzoneinfo", result="ok")
        return None


class HubCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_ports = (test_discovery.UDP_PORT, test_discovery.TCP_PORT)
        test_discovery.UDP_PORT, test_discovery.TCP_PORT = 10001, 10003
        self.unit = ProtocolUnit("127.0.0.2")
        await self.unit.start()
        hass = types.SimpleNamespace(loop=asyncio.get_running_loop())
        self.hub = hub_module.Hub(hass, "127.0.0.1", host="127.0.0.2")
        for _ in range(100):
            if self.hub._ready:
                break
            await asyncio.sleep(0.1)
        self.assertTrue(self.hub._ready)

    async def asyncTearDown(self):
        await self.hub.async_teardown()
        await self.unit.stop()
        test_discovery.UDP_PORT, test_discovery.TCP_PORT = self._old_ports

    async def test_command_is_sent_once_and_applied(self):
        await self.hub.async_send_commands({"system": "on", "type": "evap", "mode": "thermo"})
        self.assertEqual(len(self.unit.posts), 1)
        self.assertEqual(self.unit.state["system"], "on")
        self.assertEqual(self.hub.hvac_mode, "cool")

    async def test_unacknowledged_command_is_resent(self):
        self.unit.drop_posts = 1
        await self.hub.async_send_commands({"system": "on"})
        self.assertEqual(len(self.unit.posts), 2)
        self.assertEqual(self.unit.state["system"], "on")

    async def test_acknowledged_but_not_applied_command_is_resent(self):
        self.unit.ignore_posts = 1
        await self.hub.async_send_commands({"system": "on"})
        self.assertEqual(len(self.unit.posts), 2)
        self.assertEqual(self.unit.state["system"], "on")

    async def test_resend_still_works_after_an_earlier_success(self):
        # the old code remembered the first acknowledgement forever and never resent again
        await self.hub.async_send_commands({"system": "on"})
        self.unit.drop_posts = 1
        await self.hub.async_send_commands({"setPoint": "7", "mode": "thermo"})
        self.assertEqual(len(self.unit.posts), 3)
        self.assertEqual(self.unit.state["setPoint"], "7")

    async def test_gives_up_after_three_attempts_and_keeps_working(self):
        self.unit.ignore_posts = 3
        await self.hub.async_send_commands({"system": "on"})
        self.assertEqual(len(self.unit.posts), 3)
        self.assertEqual(self.hub._queued_commands, [])
        await self.hub.async_send_commands({"system": "on"})  # queue is not wedged
        self.assertEqual(self.unit.state["system"], "on")

    async def test_back_to_back_commands_are_sent_in_order(self):
        await asyncio.gather(
            self.hub.async_send_commands({"system": "on", "type": "evap", "mode": "thermo"}),
            self.hub.async_send_commands({"setPoint": 6, "mode": "thermo"}),
        )
        self.assertEqual([p.get("setPoint") for p in self.unit.posts], [None, "6"])
        self.assertEqual((self.unit.state["system"], self.unit.state["setPoint"]), ("on", "6"))


    def stale_report(self, **fields):
        """A getzoneinfo answer that was already on its way before the command was sent."""
        state = dict(self.unit.state)
        state.update(fields)
        self.hub.server_data_received(self.unit._xml(response="getzoneinfo", **state).encode())

    async def test_pending_command_is_shown_before_the_unit_confirms(self):
        task = asyncio.ensure_future(
            self.hub.async_send_commands({"system": "on", "type": "evap", "mode": "thermo"}))
        await asyncio.sleep(0)
        self.assertEqual(self.hub.hvac_mode, "cool")  # not waiting for the unit's reply
        await task
        self.assertEqual(self.hub.hvac_mode, "cool")

    async def test_stale_report_does_not_undo_a_pending_command(self):
        # The cooling defaults + helper automations read the entity and write it back about two
        # seconds later; a stale value reported meanwhile made them undo each other.
        await self.hub.async_send_commands({"system": "on", "type": "evap", "mode": "thermo"})
        self.assertEqual(self.hub.fan_mode, "5")
        task = asyncio.ensure_future(self.hub.async_send_commands({"setPoint": 7, "mode": "thermo"}))
        await asyncio.sleep(0)
        self.assertEqual(self.hub.fan_mode, "7")
        self.stale_report(setPoint="5")
        self.assertEqual(self.hub.fan_mode, "7")
        await task
        self.assertEqual(self.unit.state["setPoint"], "7")
        self.assertEqual(self.hub.fan_mode, "7")

    async def test_entity_follows_the_unit_again_once_a_command_is_finished(self):
        await self.hub.async_send_commands({"system": "on", "type": "evap", "mode": "thermo"})
        await self.hub.async_send_commands({"setPoint": 7, "mode": "thermo"})
        self.stale_report(setPoint="4")  # e.g. changed at the unit's own panel
        self.assertEqual(self.hub.fan_mode, "4")

    async def test_entity_shows_what_the_unit_reports_when_it_never_applies_a_command(self):
        self.unit.ignore_posts = 3
        await self.hub.async_send_commands({"system": "on", "type": "evap", "mode": "thermo"})
        self.assertEqual(self.hub.hvac_mode, "off")
        self.assertEqual(self.hub._queued_commands, [])

    async def test_cancelled_caller_does_not_abandon_the_queue(self):
        # an automation in "restart" mode cancels its service call when it is triggered again
        first = asyncio.ensure_future(
            self.hub.async_send_commands({"system": "on", "type": "evap", "mode": "thermo"}))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(self.hub.async_send_commands({"setPoint": 7, "mode": "thermo"}))
        await asyncio.sleep(0)
        first.cancel()
        await second
        self.assertEqual((self.unit.state["system"], self.unit.state["setPoint"]), ("on", "7"))
        self.assertEqual(self.hub._queued_commands, [])


if __name__ == "__main__":
    unittest.main()
