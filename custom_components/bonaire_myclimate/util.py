"""Home Assistant helpers for finding which networks to search for the module."""
import ipaddress
import logging
from typing import List, Optional, Tuple

from homeassistant.components import network
from homeassistant.core import HomeAssistant

from .BonairePyClimate.discovery import normalise_network

_LOGGER = logging.getLogger(__name__)

Target = Tuple[str, ipaddress.IPv4Network]


async def async_get_network_for(hass: HomeAssistant, local_ip: str) -> ipaddress.IPv4Network:
    """The IPv4 network that local_ip (our address) is on."""
    try:
        for adapter in await network.async_get_adapters(hass):
            for ipv4 in adapter["ipv4"]:
                if ipv4["address"] == local_ip:
                    return normalise_network(local_ip, ipv4["network_prefix"])
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("Could not read network adapters, assuming a /24", exc_info=True)
    return normalise_network(local_ip, 24)


async def async_get_scan_targets(hass: HomeAssistant, local_ip: Optional[str]) -> List[Target]:
    """(our address, network) for every IPv4 network Home Assistant is using, main one first."""
    targets: List[Target] = []
    try:
        for adapter in await network.async_get_adapters(hass):
            if not adapter.get("enabled"):
                continue
            for ipv4 in adapter["ipv4"]:
                address = ipaddress.IPv4Address(ipv4["address"])
                if address.is_loopback or address.is_link_local:
                    continue
                targets.append((ipv4["address"], normalise_network(ipv4["address"], ipv4["network_prefix"])))
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("Could not read network adapters", exc_info=True)

    if local_ip and not any(ip == local_ip for ip, _ in targets):
        targets.append((local_ip, await async_get_network_for(hass, local_ip)))
    targets.sort(key=lambda t: t[0] != local_ip)  # the default-route address first
    return targets
