"""A hub that connects to the Bonaire MyClimate Wi-Fi device."""
import asyncio
import logging
import xml.etree.ElementTree

from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode

from .discovery import async_send_sweep, async_send_unicast
from .helpers import (
    create_datagram_transport, create_server, zone_combinations
)
from .const import (
    FAN_MODES_COOL, FAN_MODES_EVAP, FAN_MODES_FAN_ONLY, FAN_MODES_HEAT,
    XML_DELETE, XML_DISCOVERY, XML_GETZONEINFO, XML_INSTALLATION,
)

_LOGGER = logging.getLogger(__name__)


class Hub:
    """Hub for Bonaire MyClimate."""

    def __init__(self, hass, local_ip, host=None, last_host=None, network=None,
                 on_host_found=None):
        """Init hub.

        host          -- IP address the user configured, or None to find the module automatically.
        last_host     -- IP address the module last connected back from.
        network       -- ipaddress.IPv4Network to sweep when the module cannot be found.
        on_host_found -- called with the module's IP address whenever it connects from a new one.
        """
        self._hass = hass
        self._local_ip = local_ip
        self.configured_host = host or None
        self._last_host = last_host or None
        self._network = network
        self._on_host_found = on_host_found

        self._appliances = {}
        self._callbacks = set()
        self._connected = False # When there is an open TCP connection
        self._enable_turn_on_off_backwards_compatibility = False
        self._fan_mode_memory_heat = "thermo"
        self._fan_mode_memory_evap = "manual"
        self._last_summary = None
        self._post_acked = asyncio.Event()  # unit answered our postzoneinfo with result ok
        self._zone_refreshed = asyncio.Event()  # unit answered our getzoneinfo
        self._queued_commands = []
        self._queue_task = None  # works through _queued_commands, independent of who asked
        self._ready = False # Ready to send commands
        self._start_task = None
        self._reported = {}   # what the unit last told us
        self._zone_info = {}  # what we show: _reported with the commands still on their way applied

        self._server_transport = None
        self._tcp_server = None
        self._udp_transport = None

        # Properties
        self.available = False
        self.current_temperature = None
        self.fan_mode = None
        self.fan_modes = None
        self.hvac_mode = None
        self.hvac_modes = None
        self.preset_mode = None
        self.preset_modes = None
        self.supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        self.target_temperature = None

        self._start_task = self._hass.loop.create_task(self.async_start())

    async def async_teardown(self):
        """Closes all connections and running tasks."""
        # Cancel the main loop
        if self._start_task is not None:
            self._start_task.cancel()
        if self._queue_task is not None:
            self._queue_task.cancel()

        # Close the UDP client
        if self._udp_transport is not None:
            self._udp_transport.close()

        # Send the delete request
        if self._server_transport is not None:
            _LOGGER.info("Sending delete")
            _LOGGER.debug(f"Sending: {XML_DELETE}")
            self._server_transport.write(XML_DELETE.encode())

        # Close the TCP listener
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()

    def _known_hosts(self):
        """IP addresses to send discovery to directly: configured first, then last seen."""
        return [h for h in dict.fromkeys([self.configured_host, self._last_host]) if h]

    async def async_start(self):
        """Connects to the Wi-Fi module."""
        # Create the UDP client
        self._udp_transport = await create_datagram_transport(self._hass.loop)

        # Create the TCP listener
        self._tcp_server = await create_server(self._hass.loop,
                                               self.server_connection_made,
                                               self.server_data_received,
                                               self.server_connection_lost
        )

        while True:

            attempts = 0

            while not self._connected:

                # Attempt discovery up to 5 times
                cooloff_timer = 0 if attempts < 5 else 60 if attempts < 10 else 300

                if 0 < attempts < 5:
                    _LOGGER.info(f"Discovery attempt #{attempts} failed, retrying")

                elif 5 <= attempts:
                    self.available = False
                    await self.publish_updates()
                    _LOGGER.warning(f"Discovery attempt #{attempts} failed, retrying in {cooloff_timer}s")
                    await asyncio.sleep(cooloff_timer)

                attempts += 1

                # Send the UDP discovery broadcast
                xml_discovery = XML_DISCOVERY.format(self._local_ip)
                _LOGGER.info("Sending discovery")
                _LOGGER.debug(f"Sending: {xml_discovery}")
                self._udp_transport.sendto(xml_discovery.encode())

                # The module's Wi-Fi often misses broadcasts, so also send discovery
                # straight to the address(es) it is known to have.
                known_hosts = self._known_hosts()
                if known_hosts:
                    _LOGGER.debug(f"Sending discovery to {known_hosts}")
                    await async_send_unicast(self._local_ip, known_hosts)

                # Still not found after a few tries (e.g. its address changed): try every
                # address on the subnet, then again every fifth attempt.
                if self._network is not None and attempts >= 3 and (attempts - 3) % 5 == 0:
                    _LOGGER.info(f"Module not found, scanning {self._network} for it")
                    await async_send_sweep(self._local_ip, self._network)

                # Wait for 10 seconds for a response to the discovery broadcast
                await asyncio.sleep(10)

            await asyncio.sleep(220)
            self._connected = False
            self._ready = False

            # Send the delete request
            _LOGGER.info("Sending delete")
            _LOGGER.debug(f"Sending: {XML_DELETE}")
            self._server_transport.write(XML_DELETE.encode())

            await asyncio.sleep(3)

    def register_callback(self, callback):
        """Register callback, called when device changes state."""
        self._callbacks.add(callback)

    def remove_callback(self, callback):
        """Remove previously registered callback."""
        self._callbacks.discard(callback)

    async def publish_updates(self):
        """Schedule call all registered callbacks."""
        for callback in self._callbacks:
            callback()

    def server_connection_made(self, transport):
        _LOGGER.info("Connected to the Bonaire MyClimate Wi-Fi device")
        self._server_transport = transport

        # Remember where the module is, so discovery can go straight to it next time
        peer = transport.get_extra_info("peername")
        if peer and peer[0] != self._last_host:
            self._last_host = peer[0]
            _LOGGER.info(f"Module is at {self._last_host}")
            if self._on_host_found is not None:
                self._on_host_found(self._last_host)

    def server_data_received(self, data):
        message = data.decode()
        _LOGGER.info("Server data received")
        _LOGGER.debug(f"Received: {message}")
        try:
            root = xml.etree.ElementTree.fromstring(message)
        except xml.etree.ElementTree.ParseError:
            _LOGGER.warning(f"Ignoring unreadable message from the module: {message!r}")
            return

        # Check if the message is a discovery response
        if root.findtext("response") == "discovery":

            self._connected = True

            # Send the installation request
            _LOGGER.info("Sending installation")
            _LOGGER.debug(f"Sending: {XML_INSTALLATION}")
            self._server_transport.write(XML_INSTALLATION.encode())

        # Check if the message is an installation response
        elif root.findtext("response") == "installation":

            # Store the zoneList for each 'appliance' (i.e. heat, cool, evap)
            for appliance in root.findall('appliance'):
                type = appliance.find('type').text
                zoneList = appliance.find('zoneList').text
                self._appliances[type] = zoneList

            # Build hvac_modes property
            self.hvac_modes = [HVACMode.OFF, HVACMode.FAN_ONLY]
            if self._appliances["heat"] is not None:
                self.hvac_modes += [HVACMode.HEAT]
            if (self._appliances["cool"] is not None or
                    self._appliances["evap"] is not None):
                self.hvac_modes += [HVACMode.COOL]

            # Build the preset modes for each type of appliance
            self._preset_modes_heat = zone_combinations(
                                          self._appliances["heat"])
            self._preset_modes_cool = zone_combinations(
                                          self._appliances["cool"])
            self._preset_modes_evap = zone_combinations(
                                          self._appliances["evap"])

            # Send the getzoneinfo request
            _LOGGER.info("Sending getzoneinfo")
            _LOGGER.debug(f"Sending: {XML_GETZONEINFO}")
            self._server_transport.write(XML_GETZONEINFO.encode())

        # Check if the message is a getzoneinfo response or postzoneinfo
        elif (root.findtext("response") == "getzoneinfo" or
                root.findtext("post") == "postzoneinfo"):

            # Save all the hvac zone info
            self._reported = {}
            for item in root.iterfind("./"):
                self._reported[item.tag] = item.text
            self._remember_modes(self._reported)
            self._rebuild_state()

            self.available = True
            self._ready = True
            self._log_summary()
            if root.findtext("response") == "getzoneinfo":
                self._zone_refreshed.set()
            self._hass.loop.create_task(self.publish_updates())

        # Check if the message is postzoneinfo result 'ok'
        elif (root.findtext("response") == "postzoneinfo" and
                root.findtext("result") == "ok"):

            self._post_acked.set()
            _LOGGER.info("Sending getzoneinfo")
            _LOGGER.debug(f"Sending: {XML_GETZONEINFO}")
            self._server_transport.write(XML_GETZONEINFO.encode())

    def _remember_modes(self, z):
        """Remember the last heat / evap mode, so turning the system on again can restore it."""
        if z.get("system") == "off" or z.get("mode") == "fan":
            return
        if z.get("type") == "heat":
            self._fan_mode_memory_heat = z["mode"]
        elif z.get("type") == "evap":
            self._fan_mode_memory_evap = z["mode"]

    def _properties_from(self, z):
        """The climate properties for a given zone info (raises KeyError if it is incomplete)."""
        props = {}
        if z["system"] == "off":
            props["hvac_mode"] = HVACMode.OFF
        elif z["mode"] == "fan":
            props["fan_mode"] = z["fanSpeed"]
            props["fan_modes"] = FAN_MODES_FAN_ONLY
            props["hvac_mode"] = HVACMode.FAN_ONLY
            if z["type"] == "heat":
                props["preset_modes"] = self._preset_modes_heat
            else:
                props["preset_modes"] = self._preset_modes_cool
            props["supported_features"] = ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        elif z["type"] == "heat":
            props["fan_mode"] = z["mode"]
            props["fan_modes"] = FAN_MODES_HEAT
            props["hvac_mode"] = HVACMode.HEAT
            props["preset_modes"] = self._preset_modes_heat
            props["supported_features"] = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
            props["target_temperature"] = int(z["setPoint"])
        elif z["type"] == "cool":
            props["fan_mode"] = z["mode"]
            props["fan_modes"] = FAN_MODES_COOL
            props["hvac_mode"] = HVACMode.COOL
            props["preset_modes"] = self._preset_modes_cool
            props["supported_features"] = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
            props["target_temperature"] = int(z["setPoint"])
        elif z["type"] == "evap" and z["mode"] == "thermo":
            props["fan_mode"] = z["setPoint"]
            props["fan_modes"] = FAN_MODES_EVAP
            props["hvac_mode"] = HVACMode.COOL
            props["preset_modes"] = ["thermo", "manual", "boost"]
            props["supported_features"] = ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        elif z["type"] == "evap":
            props["fan_mode"] = z["fanSpeed"]
            props["fan_modes"] = FAN_MODES_EVAP
            props["hvac_mode"] = HVACMode.COOL
            props["preset_modes"] = ["thermo", "manual", "boost"]
            props["supported_features"] = ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON

        props["current_temperature"] = int(z["roomTemp"])
        if z.get("type") == "evap":
            props["preset_mode"] = z["mode"]
        else:
            props["preset_mode"] = z["zoneList"]
        return props

    def _rebuild_state(self):
        """Set the climate properties: what the unit reports, plus the commands on their way to it.

        A command that has been given to us but that the unit has not yet confirmed is shown as
        already applied.  Otherwise a report that was already on its way when the command was sent
        puts the old value back for a few seconds, and automations that copy the entity to a helper
        and write it back (as the cooling defaults do) then send that stale value and undo the
        command.  Once the unit has confirmed the command, or we have given up on it, the entity
        simply shows what the unit reports.
        """
        view = dict(self._reported)
        for command in self._queued_commands:
            view.update({key: str(value) for key, value in command.items()})

        props = None
        if view != self._reported:
            try:
                props = self._properties_from(view)
            except (KeyError, ValueError, TypeError):
                # Not enough is known to show the pending command (e.g. nothing reported yet)
                view = dict(self._reported)
        if props is None:
            props = self._properties_from(view)

        self._zone_info = view
        for name, value in props.items():
            setattr(self, name, value)

    def _log_summary(self):
        """Log what the unit says its state is (INFO when it changes, DEBUG otherwise)."""
        z = self._reported
        summary = ", ".join(f"{k}={z.get(k)}" for k in
                            ("system", "type", "mode", "setPoint", "fanSpeed", "zoneList", "roomTemp"))
        level = logging.DEBUG if summary == self._last_summary else logging.INFO
        self._last_summary = summary
        _LOGGER.log(level, f"Unit reports: {summary}")

    def _command_applied(self, command):
        """True if the unit's reported state matches every setting in the command.

        Settings the unit does not report back cannot be checked and are ignored.
        """
        return all(str(self._reported.get(key)) == str(value)
                   for key, value in command.items() if key in self._reported)

    def server_connection_lost(self):
        _LOGGER.debug("Server connection lost")
        self._connected = False
        self._ready = False

    async def async_set_fan_mode(self, fan_mode):
        """Set new target fan operation."""
        if self._zone_info["mode"] == "fan":
            command = {"fanSpeed": fan_mode}
        elif self._zone_info["type"] == "evap" and self._zone_info["mode"] == "thermo":
            command = {"setPoint": fan_mode}
            command["mode"] = "thermo"
        elif self._zone_info["type"] == "evap":
            command = {"fanSpeed": fan_mode}
            if int(fan_mode) < 8:
                command["mode"] = "manual"
            elif int(fan_mode) == 8:
                command["mode"] = "boost"
        else:
            command = {"mode": fan_mode}

        await self.async_send_commands(command)

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new target hvac mode."""
        if hvac_mode == HVACMode.OFF:
            commands = {
                "system": "off"
            }
        elif hvac_mode == HVACMode.FAN_ONLY:
            commands = {
                "system": "on",
                "mode": "fan"
            }
        elif hvac_mode == HVACMode.HEAT:
            commands = {
                "system": "on",
                "type": "heat",
                "mode": self._fan_mode_memory_heat
            }
        elif hvac_mode == HVACMode.COOL:
            if self._appliances["cool"] is not None:
                commands = {
                    "system": "on",
                    "type": "cool",
                    "mode": "thermo"
                }
            elif self._appliances["evap"] is not None:
                commands = {
                    "system": "on",
                    "type": "evap",
                    "mode": self._fan_mode_memory_evap
                }

        await self.async_send_commands(commands)

    async def async_turn_on(self) -> None:
        command = {"system": "on"}
        await self.async_send_commands(command)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_preset_mode(self, preset_mode):
        """Set new target preset mode."""
        if self._zone_info.get("type") == "evap":
            command = {"type": "evap", "mode": preset_mode}
        else:
            command = {"zoneList": preset_mode}
        await self.async_send_commands(command)

    async def async_set_temperature(self, temperature):
        """Set new target temperature."""
        command = {"setPoint": str(temperature)}
        await self.async_send_commands(command)

    async def async_send_commands(self, command):
        """Send commands to device.

        Each command is sent, the unit's acknowledgement is waited for, and then the
        unit's reported state is checked to see that it really took the change.  If
        either is missing the command is sent again (up to 3 times in total).
        """
        _LOGGER.info(command)
        # Append the given command to the end of the queued commands
        self._queued_commands.append(command)
        self._show_pending_commands()

        # The queue is worked through by its own task, so a caller that is cancelled (e.g. an
        # automation in "restart" mode) does not abandon the commands still waiting.
        if self._queue_task is None or self._queue_task.done():
            self._queue_task = self._hass.loop.create_task(self._async_send_queued())
        await asyncio.shield(self._queue_task)

    async def _async_send_queued(self):
        """Send the queued commands one after another."""
        try:
            while len(self._queued_commands) > 0:
                await self._async_send_one(self._queued_commands[0])
                self._queued_commands.pop(0)
                self._show_pending_commands()
        finally:
            # Never leave stale commands behind: a non-empty queue blocks all later sends
            self._queued_commands.clear()
            self._show_pending_commands()

    def _show_pending_commands(self):
        """Update the entity for a change in the commands that are waiting for the unit."""
        if not self._reported:
            return  # nothing known about the unit yet
        before = self._zone_info
        self._rebuild_state()
        if self._zone_info != before:
            self._hass.loop.create_task(self.publish_updates())

    async def _async_send_one(self, command):
        """Send one command and make sure the unit applied it."""
        postzoneinfo = "<myclimate><post>postzoneinfo</post>"
        for key, value in command.items():
            postzoneinfo += f"<{key}>{value}</{key}>"
        postzoneinfo += "</myclimate>"

        for attempt in range(3):
            # Wait until ready to send commands (the connection is re-made every few minutes)
            while not self._ready:
                await asyncio.sleep(1)

            if attempt > 0:
                _LOGGER.info("Resending postzoneinfo")
            else:
                _LOGGER.info("Sending postzoneinfo")
            _LOGGER.debug(f"Sending: {postzoneinfo}")

            self._post_acked.clear()
            self._zone_refreshed.clear()
            try:
                self._server_transport.write(postzoneinfo.encode())
            except (AttributeError, OSError) as err:
                _LOGGER.warning(f"Could not send postzoneinfo: {err}")
                await asyncio.sleep(3)
                continue

            # The unit acknowledges the post, then we ask for its state (see server_data_received)
            try:
                await asyncio.wait_for(self._post_acked.wait(), 4)
                await asyncio.wait_for(self._zone_refreshed.wait(), 5)
            except asyncio.TimeoutError:
                _LOGGER.info("Response not received from the module")
                continue

            if self._command_applied(command):
                return
            _LOGGER.info("The module acknowledged the command but reports a different state")

        _LOGGER.warning(f"The module did not apply {command} after 3 attempts; it reports: "
                        f"{self._last_summary}")
