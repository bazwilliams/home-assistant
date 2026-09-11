"""Shared event notify server for the Openhome integration."""

import asyncio
from collections import defaultdict

from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpSessionRequester
from async_upnp_client.event_handler import UpnpEventHandler
from async_upnp_client.utils import async_get_local_ip

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

DATA_KEY = f"{DOMAIN}_notify"


class OpenhomeNotifyServers:
    """One notify server per local address, shared by every device on it.

    Devices each subscribe to several services, so without this each one
    would listen on a port of its own.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the store."""
        self._hass = hass
        self._lock = asyncio.Lock()
        self._servers: dict[str, AiohttpNotifyServer] = {}
        self._references: defaultdict[str, int] = defaultdict(int)
        self._stop_listener: CALLBACK_TYPE | None = None

    async def async_acquire(self, location: str) -> tuple[str, UpnpEventHandler]:
        """Return the address and event handler to use for a device.

        Release it with async_release when the device is done with it.
        """
        _, local_ip = await async_get_local_ip(location)

        async with self._lock:
            if self._stop_listener is None:
                self._stop_listener = self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STOP, self._async_stop_all
                )

            self._references[local_ip] += 1

            if (server := self._servers.get(local_ip)) is None:
                session = async_get_clientsession(self._hass)
                server = AiohttpNotifyServer(
                    requester=AiohttpSessionRequester(session),
                    # Port 0: a fixed one would stop a second server listening.
                    source=(local_ip, 0),
                    loop=self._hass.loop,
                )
                await server.async_start_server()
                self._servers[local_ip] = server

        return local_ip, server.event_handler

    async def async_release(self, local_ip: str) -> None:
        """Give up a device's claim, stopping the server once none are left."""
        async with self._lock:
            self._references[local_ip] -= 1
            if self._references[local_ip] > 0:
                return

            del self._references[local_ip]
            if (server := self._servers.pop(local_ip, None)) is not None:
                await server.async_stop_server()

            if not self._servers and self._stop_listener is not None:
                self._stop_listener()
                self._stop_listener = None

    async def _async_stop_all(self, event: Event) -> None:
        """Stop every server when Home Assistant shuts down."""
        async with self._lock:
            for server in self._servers.values():
                await server.async_stop_server()
            self._servers.clear()
            self._references.clear()
            self._stop_listener = None


def async_get_notify_servers(hass: HomeAssistant) -> OpenhomeNotifyServers:
    """Return the shared notify servers, creating them on first use."""
    if (servers := hass.data.get(DATA_KEY)) is None:
        servers = hass.data[DATA_KEY] = OpenhomeNotifyServers(hass)
    return servers
