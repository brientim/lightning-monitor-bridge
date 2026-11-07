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
import threading
from dataclasses import dataclass

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


@dataclass(frozen=True)
class SharedDetector:
    """A detector shared on the network: by Lightning Monitor on a Mac, ``ld350-proxy`` or another bridge."""

    name: str
    host: str
    port: int
    description: str
    requires_token: bool


def shared_detector(service_name: str, properties: dict, server: str | None, port: int | None) -> SharedDetector | None:
    """Reads a ``_lightningmon._tcp`` record like Lightning Monitor's ``SharedDetectorBrowser``: the TXT
    ``host`` and ``port``, falling back to the service's own; records without both are ignored."""
    text = {
        (key.decode("utf-8", "replace") if isinstance(key, bytes) else key):
        (value.decode("utf-8", "replace") if isinstance(value, bytes) else value or "")
        for key, value in properties.items()
    }
    host = text.get("host") or (server or "").rstrip(".")
    try:
        port = int(text.get("port") or port or 0)
    except ValueError:
        return None
    if not host or not 1 <= port <= 65535:
        return None
    name = service_name[: -len(SERVICE_TYPE) - 1] if service_name.endswith("." + SERVICE_TYPE) else service_name
    return SharedDetector(name, host, port, text.get("server", ""), text.get("auth") == "1")


class SharedDetectorBrowser:
    """Keeps a list of the detectors shared on the network, leaving out this bridge's own."""

    def __init__(self, on_change=None):
        """``on_change()`` is called, on the browser's thread, when the list changes."""
        self.found: dict[str, SharedDetector] = {}
        self.lock = threading.Lock()
        self.notify = on_change
        self.zeroconf = None
        self.browser = None

    def start(self) -> None:
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            logger.warning("zeroconf is not installed; shared detectors can't be listed")
            return
        try:
            self.zeroconf = Zeroconf()
            self.browser = ServiceBrowser(self.zeroconf, SERVICE_TYPE, handlers=[self.on_change])
        except Exception as error:  # noqa: BLE001 - mDNS problems must not stop the tray
            logger.warning("Could not look for shared detectors: %s", error)

    def on_change(self, zeroconf, service_type, name, state_change) -> None:
        from zeroconf import ServiceStateChange

        detector = None
        if state_change is not ServiceStateChange.Removed:
            info = zeroconf.get_service_info(service_type, name, timeout=3000)
            detector = info and shared_detector(name, info.properties or {}, info.server, info.port)
        with self.lock:
            before = self.found.get(name)
            if detector and detector.host.lower() != mdns_host_name().lower():
                self.found[name] = detector
            else:
                self.found.pop(name, None)
            changed = self.found.get(name) != before
        if changed and self.notify:
            self.notify()

    def detectors(self) -> list[SharedDetector]:
        with self.lock:
            return sorted(self.found.values(), key=lambda detector: detector.name.lower())

    def stop(self) -> None:
        if self.zeroconf is not None:
            self.zeroconf.close()
            self.zeroconf = None


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
