# SPDX-License-Identifier: GPL-3.0-or-later
"""Bonjour (mDNS / DNS-SD) announcement, so Lightning Monitor lists the bridge under "On this network".

Matches Lightning Monitor's own sharing (LD350Kit ``SharedDetectorBrowser``): service type
``_lightningmon._tcp`` and TXT keys ``server`` (description), ``host``, ``port`` and ``auth`` ("1" when a
token is needed). ``host`` is a ``<computer>-bridge.local`` name that this process answers itself, so it
resolves on Windows and Linux too and does not clash with the computer's own mDNS name.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket

SERVICE_TYPE = "_lightningmon._tcp.local."
REFRESH_SECONDS = 60

logger = logging.getLogger("lightning_bridge")


def computer_name() -> str:
    name = socket.gethostname().split(".")[0]
    return name or "Lightning Monitor Bridge"


def mdns_host_name(name: str | None = None) -> str:
    """e.g. "Weather-PC" -> "weather-pc-bridge.local"."""
    label = re.sub(r"[^a-z0-9-]+", "-", (name or computer_name()).lower()).strip("-") or "lightning"
    return "%s-bridge.local" % label[:50]


def local_addresses() -> list[str]:
    """IPv4 addresses other devices can reach: not loopback or link-local."""
    import ifaddr

    addresses = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue
            address = ipaddress.ip_address(ip.ip)
            if address.is_loopback or address.is_link_local:
                continue
            addresses.append(ip.ip)
    return sorted(set(addresses))


def txt_properties(description: str, host: str, port: int, requires_token: bool) -> dict[str, str]:
    return {"server": description, "host": host, "port": str(port), "auth": "1" if requires_token else "0"}


def service_info(name: str, description: str, port: int, requires_token: bool, addresses: list[str]):
    from zeroconf import ServiceInfo

    host = mdns_host_name()
    return ServiceInfo(
        SERVICE_TYPE,
        "%s.%s" % (name, SERVICE_TYPE),
        port=port,
        properties=txt_properties(description, host, port, requires_token),
        server=host + ".",
        addresses=[socket.inet_aton(address) for address in addresses],
    )


class Advertiser:
    """Announces the bridge while ``run()`` is awaited; re-announces when the addresses change."""

    def __init__(self, name: str, description: str, port: int, requires_token: bool):
        self.name = name or computer_name()
        self.description = description
        self.port = port
        self.requires_token = requires_token

    async def run(self, stopped: asyncio.Event) -> None:
        try:
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError:
            logger.warning("zeroconf is not installed; the bridge will not appear under On this network")
            return
        zeroconf = AsyncZeroconf()
        addresses = local_addresses()
        info = service_info(self.name, self.description, self.port, self.requires_token, addresses)
        try:
            await zeroconf.async_register_service(info, allow_name_change=True)
            logger.info("Announced on the network as %r (%s, %s)", self.name, mdns_host_name(), ", ".join(addresses) or "no address")
            while not stopped.is_set():
                try:
                    await asyncio.wait_for(stopped.wait(), REFRESH_SECONDS)
                except asyncio.TimeoutError:
                    current = local_addresses()
                    if current != addresses:
                        addresses = current
                        # Keep the registered (possibly renamed) ServiceInfo; only its addresses change.
                        info.addresses = [socket.inet_aton(address) for address in addresses]
                        await zeroconf.async_update_service(info)
                        logger.info("Network addresses changed: %s", ", ".join(addresses) or "none")
        except Exception as error:  # noqa: BLE001 - mDNS problems must not stop sharing
            logger.warning("Could not announce on the network: %s", error)
        finally:
            try:
                await zeroconf.async_unregister_all_services()
            finally:
                await zeroconf.async_close()
