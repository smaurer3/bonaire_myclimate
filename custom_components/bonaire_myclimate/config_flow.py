"""Config flow for Bonaire MyClimate integration."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.network import async_get_source_ip
from homeassistant.const import CONF_HOST
from homeassistant.core import callback

from .BonairePyClimate.discovery import async_scan, is_valid_ipv4
from .const import CONF_LAST_HOST, CONF_SERIAL, DEVICE_NAME, DOMAIN
from .util import async_get_scan_targets

_LOGGER = logging.getLogger(__name__)

# How long to wait for the module: a direct probe of a typed-in address, or a full network scan.
PROBE_TIMEOUT = 12  # the module can take ~5 s to answer after it has been released
SCAN_TIMEOUT = 25


def _host_schema(suggested):
    return vol.Schema({vol.Optional(CONF_HOST, description={"suggested_value": suggested}): str})


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bonaire MyClimate."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    def __init__(self):
        self._unit = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Let the user change the module's IP address later."""
        return OptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Ask for the module's IP address, or scan the network for it if left blank."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors = {}
        host = ""
        if user_input is not None:
            host = (user_input.get(CONF_HOST) or "").strip()
            if host and not is_valid_ipv4(host):
                errors[CONF_HOST] = "invalid_host"
            else:
                local_ip = await async_get_source_ip(self.hass)
                targets = await async_get_scan_targets(self.hass, local_ip)
                try:
                    if host:
                        # Typed in: talk to that address only
                        unit = await async_scan(targets, hosts=[host], sweep=False, timeout=PROBE_TIMEOUT)
                    else:
                        unit = await async_scan(targets, timeout=SCAN_TIMEOUT)
                except OSError as err:
                    _LOGGER.warning("Could not listen for the module: %s", err)
                    errors["base"] = "port_in_use"
                else:
                    if unit is None:
                        errors["base"] = "cannot_connect" if host else "not_found"
                    elif host:
                        return await self._async_create(unit, host)
                    else:
                        self._unit = unit
                        return await self.async_step_confirm()

        return self.async_show_form(
            step_id="user", data_schema=_host_schema(host), errors=errors
        )

    async def async_step_confirm(self, user_input=None):
        """A module was found by scanning: confirm adding it."""
        if user_input is not None:
            return await self._async_create(self._unit, None)

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "host": self._unit.host,
                "serial": self._unit.serial or "unknown",
            },
        )

    async def _async_create(self, unit, host):
        data = {CONF_LAST_HOST: unit.host}
        if host:
            data[CONF_HOST] = host
        if unit.serial:
            data[CONF_SERIAL] = unit.serial
            await self.async_set_unique_id(unit.serial)
            self._abort_if_unique_id_configured()
        return self.async_create_entry(title=DEVICE_NAME, data=data)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Change the module's IP address (blank = find it automatically)."""

    async def async_step_init(self, user_input=None):
        errors = {}
        current = self.config_entry.data.get(CONF_HOST, "")
        if user_input is not None:
            host = (user_input.get(CONF_HOST) or "").strip()
            if host and not is_valid_ipv4(host):
                errors[CONF_HOST] = "invalid_host"
                current = host
            else:
                data = dict(self.config_entry.data)
                if host:
                    data[CONF_HOST] = host
                else:
                    data.pop(CONF_HOST, None)
                # The integration reloads itself when the address changes
                self.hass.config_entries.async_update_entry(self.config_entry, data=data)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init", data_schema=_host_schema(current), errors=errors
        )
