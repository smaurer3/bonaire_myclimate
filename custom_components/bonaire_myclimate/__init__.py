"""The Bonaire MyClimate integration."""
import asyncio

from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback

from .BonairePyClimate import hub
from .const import CONF_LAST_HOST, DOMAIN
from .util import async_get_network_for

PLATFORMS = ["climate"]


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Bonaire MyClimate component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Bonaire MyClimate from a config entry."""
    local_ip = await async_get_source_ip(hass)
    network = await async_get_network_for(hass, local_ip)

    @callback
    def host_found(host):
        """The module connected from a (new) address: remember it for next time."""
        if entry.data.get(CONF_LAST_HOST) != host:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_LAST_HOST: host}
            )

    hass.data[DOMAIN][entry.entry_id] = hub.Hub(
        hass,
        local_ip,
        host=entry.data.get(CONF_HOST),
        last_host=entry.data.get(CONF_LAST_HOST),
        network=network,
        on_host_found=host_found,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload when the user changes the address in the options (but not when we only
    # updated the remembered "last_host" ourselves).
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    async def send_raw_command(call):
        """Handle the service call."""
        command = call.data.get("raw_command")

        await hass.data[DOMAIN][entry.entry_id].async_send_commands(command)

    hass.services.async_register(DOMAIN, "send_raw_command", send_raw_command)

    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Config entry data changed."""
    current = hass.data[DOMAIN].get(entry.entry_id)
    if current is not None and (entry.data.get(CONF_HOST) or None) != current.configured_host:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
