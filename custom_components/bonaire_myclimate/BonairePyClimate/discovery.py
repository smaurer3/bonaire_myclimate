"""Find the Bonaire MyClimate Wi-Fi module on the local network.

The module is told where Home Assistant is by a "discovery" message sent over UDP to
port 10001; it replies by opening a TCP connection back to the sender on port 10003.

A UDP *broadcast* is the app's way of finding the module, but on many Wi-Fi networks the
module misses broadcasts (power save / DTIM, AP broadcast filtering, switch storm control)
even though it answers a *unicast* message to its own IP immediately.  So, besides the
broadcast, this module can send discovery:

* directly to IP addresses we already know about (configured or last seen), and
* to every address on the local subnet (a "sweep") when nothing else works.

This file deliberately has no Home Assistant imports so it can be tested on its own.
"""
import asyncio
import ipaddress
import logging
import socket
import xml.etree.ElementTree
from typing import List, NamedTuple, Optional, Sequence, Tuple

from .const import PORT_DISCOVERY, PORT_LOCAL, XML_DELETE, XML_DISCOVERY

_LOGGER = logging.getLogger(__name__)

# Never sweep more than a /22 worth of addresses.
MAX_SWEEP_HOSTS = 1024
# Pause between sweep packets so we don't flood a small Wi-Fi module / AP.
SWEEP_DELAY = 0.005


class UnitInfo(NamedTuple):
    """What the module told us about itself."""

    host: str
    serial: Optional[str]
    version: Optional[str]


def parse_discovery_response(message: str, peer: Optional[str] = None) -> Optional[UnitInfo]:
    """Parse the module's discovery response, or return None if it is not one."""
    try:
        root = xml.etree.ElementTree.fromstring(message)
    except xml.etree.ElementTree.ParseError:
        return None
    if root.findtext("response") != "discovery":
        return None
    host = peer or root.findtext("ip")
    if not host:
        return None
    return UnitInfo(host, root.findtext("serial"), root.findtext("version"))


def normalise_network(local_ip: str, prefix: int = 24) -> ipaddress.IPv4Network:
    """The IPv4 network to sweep for an interface address, never bigger than a /22."""
    if prefix < 22:
        prefix = 24
    return ipaddress.ip_network(f"{local_ip}/{prefix}", strict=False)


def is_valid_ipv4(value: str) -> bool:
    """True if value is a plain dotted-quad IPv4 address."""
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def _udp_socket(local_ip: Optional[str]) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    if local_ip:
        sock.bind((local_ip, 0))
    return sock


def _send(sock: socket.socket, payload: bytes, addr: Tuple[str, int]) -> None:
    try:
        sock.sendto(payload, addr)
    except OSError as err:  # includes BlockingIOError; UDP is best-effort
        _LOGGER.debug("Could not send discovery to %s: %s", addr, err)


async def async_send_unicast(local_ip: Optional[str], hosts: Sequence[str],
                             port: int = PORT_DISCOVERY) -> None:
    """Send the discovery message straight to each of the given IP addresses."""
    if not hosts:
        return
    payload = XML_DISCOVERY.format(local_ip or "").encode()
    sock = _udp_socket(local_ip)
    try:
        for host in hosts:
            _send(sock, payload, (host, port))
    finally:
        sock.close()


async def async_send_sweep(local_ip: Optional[str], network: ipaddress.IPv4Network,
                           port: int = PORT_DISCOVERY) -> None:
    """Send the discovery message to every address on the network."""
    payload = XML_DISCOVERY.format(local_ip or "").encode()
    sock = _udp_socket(local_ip)
    try:
        count = 0
        for host in network.hosts():
            if str(host) == local_ip:
                continue
            _send(sock, payload, (str(host), port))
            count += 1
            if count >= MAX_SWEEP_HOSTS:
                break
            await asyncio.sleep(SWEEP_DELAY)
    finally:
        sock.close()


async def _wait(future: "asyncio.Future", timeout: float):
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout)
    except asyncio.TimeoutError:
        return None


async def async_scan(
    targets: Sequence[Tuple[Optional[str], Optional[ipaddress.IPv4Network]]],
    hosts: Sequence[str] = (),
    sweep: bool = True,
    timeout: float = 20.0,
    quick_wait: float = 4.0,
    port_discovery: int = PORT_DISCOVERY,
    port_local: int = PORT_LOCAL,
) -> Optional[UnitInfo]:
    """Look for the module and return what it says about itself, or None.

    targets  -- (our IP on that network, the network) for each interface to search from.
    hosts    -- IP addresses to try directly (configured / last seen).
    sweep    -- if the quick attempt (broadcast + hosts) gets no answer after quick_wait
                seconds, try every address on each network.

    Opens a TCP listener on port_local for the module to connect back to, so this must
    not be used while the integration itself is running (it raises OSError if the port is
    busy).  The module is told to drop the connection afterwards so the integration can
    take it over.
    """
    loop = asyncio.get_running_loop()
    found: "asyncio.Future" = loop.create_future()

    async def handle(reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(4096), 5)
            peer = writer.get_extra_info("peername")
            info = parse_discovery_response(data.decode(errors="ignore"), peer[0] if peer else None)
            if info and not found.done():
                found.set_result(info)
                writer.write(XML_DELETE.encode())
                await writer.drain()
        except (asyncio.TimeoutError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host="0.0.0.0", port=port_local)
    socks: List[Tuple[Optional[str], Optional[ipaddress.IPv4Network], socket.socket]] = []
    sweeps: List["asyncio.Task"] = []
    try:
        for local_ip, network in (targets or [(None, None)]):
            socks.append((local_ip, network, _udp_socket(local_ip)))

        # Quick attempt: the known addresses and a broadcast, from every interface.
        for local_ip, network, sock in socks:
            payload = XML_DISCOVERY.format(local_ip or "").encode()
            for host in hosts:
                _send(sock, payload, (host, port_discovery))
            _send(sock, payload, ("255.255.255.255", port_discovery))
            if network is not None:
                _send(sock, payload, (str(network.broadcast_address), port_discovery))

        info = await _wait(found, quick_wait if sweep else timeout)
        if info is not None or not sweep:
            return info

        # Slow attempt: every address on every network.
        for local_ip, network, _ in socks:
            if network is not None:
                sweeps.append(loop.create_task(async_send_sweep(local_ip, network, port_discovery)))
        return await _wait(found, max(timeout - quick_wait, 1.0))
    finally:
        for task in sweeps:
            task.cancel()
        for _, _, sock in socks:
            sock.close()
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), 5)
        except asyncio.TimeoutError:
            pass
