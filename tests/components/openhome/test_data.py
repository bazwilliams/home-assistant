"""Tests for the shared Openhome notify server."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.openhome.data import (
    OpenhomeNotifyServers,
    async_get_notify_servers,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

FIRST_IP = "192.0.2.1"
SECOND_IP = "192.0.2.2"

FIRST_LOCATION = "http://192.0.2.10/device.xml"
SECOND_LOCATION = "http://192.0.2.11/device.xml"
FAR_LOCATION = "http://198.51.100.10/device.xml"

# Which local address each device is reached from, standing in for the
# routing table async_get_local_ip consults.
LOCAL_IPS = {
    FIRST_LOCATION: FIRST_IP,
    SECOND_LOCATION: FIRST_IP,
    FAR_LOCATION: SECOND_IP,
}


@pytest.fixture(autouse=True)
def mock_local_ip() -> Generator[None]:
    """Route each device location to a local address without a socket."""
    with patch(
        "homeassistant.components.openhome.data.async_get_local_ip",
        AsyncMock(side_effect=lambda location: (None, LOCAL_IPS[location])),
    ):
        yield


@pytest.fixture
def notify_servers() -> Generator[list[MagicMock]]:
    """Return the servers started so far, newest last."""
    started: list[MagicMock] = []

    def make_server(**kwargs: object) -> MagicMock:
        server = MagicMock()
        server.async_start_server = AsyncMock()
        server.async_stop_server = AsyncMock()
        started.append(server)
        return server

    with patch(
        "homeassistant.components.openhome.data.AiohttpNotifyServer",
        MagicMock(side_effect=make_server),
    ):
        yield started


async def test_devices_on_one_address_share_a_server(
    hass: HomeAssistant, notify_servers: list[MagicMock]
) -> None:
    """Test a second device on the same address does not start a server."""
    servers = OpenhomeNotifyServers(hass)

    first_ip, first_handler = await servers.async_acquire(FIRST_LOCATION)
    second_ip, second_handler = await servers.async_acquire(SECOND_LOCATION)

    assert first_ip == second_ip == FIRST_IP
    assert first_handler is second_handler
    assert len(notify_servers) == 1
    notify_servers[0].async_start_server.assert_awaited_once()


async def test_devices_on_different_addresses_get_their_own(
    hass: HomeAssistant, notify_servers: list[MagicMock]
) -> None:
    """Test a device reached from another address listens on that one."""
    servers = OpenhomeNotifyServers(hass)

    first_ip, first_handler = await servers.async_acquire(FIRST_LOCATION)
    far_ip, far_handler = await servers.async_acquire(FAR_LOCATION)

    assert (first_ip, far_ip) == (FIRST_IP, SECOND_IP)
    assert first_handler is not far_handler
    assert len(notify_servers) == 2


async def test_server_outlives_all_but_the_last_device(
    hass: HomeAssistant, notify_servers: list[MagicMock]
) -> None:
    """Test the shared server is only stopped once nothing is using it."""
    servers = OpenhomeNotifyServers(hass)
    await servers.async_acquire(FIRST_LOCATION)
    await servers.async_acquire(SECOND_LOCATION)

    await servers.async_release(FIRST_IP)

    # The other device is still on it and still receiving events.
    notify_servers[0].async_stop_server.assert_not_awaited()

    await servers.async_release(FIRST_IP)

    notify_servers[0].async_stop_server.assert_awaited_once()


async def test_releasing_one_address_leaves_the_other_listening(
    hass: HomeAssistant, notify_servers: list[MagicMock]
) -> None:
    """Test giving up one address does not stop the server on another."""
    servers = OpenhomeNotifyServers(hass)
    await servers.async_acquire(FIRST_LOCATION)
    await servers.async_acquire(FAR_LOCATION)

    await servers.async_release(FIRST_IP)

    first, far = notify_servers
    first.async_stop_server.assert_awaited_once()
    far.async_stop_server.assert_not_awaited()


async def test_reacquiring_after_release_starts_a_new_server(
    hass: HomeAssistant, notify_servers: list[MagicMock]
) -> None:
    """Test the address can be used again once its server has been stopped."""
    servers = OpenhomeNotifyServers(hass)
    await servers.async_acquire(FIRST_LOCATION)
    await servers.async_release(FIRST_IP)

    await servers.async_acquire(FIRST_LOCATION)

    assert len(notify_servers) == 2
    notify_servers[1].async_start_server.assert_awaited_once()


async def test_shutdown_stops_every_server(
    hass: HomeAssistant, notify_servers: list[MagicMock]
) -> None:
    """Test Home Assistant stopping takes the notify servers down with it."""
    servers = OpenhomeNotifyServers(hass)
    await servers.async_acquire(FIRST_LOCATION)
    await servers.async_acquire(FAR_LOCATION)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert len(notify_servers) == 2
    for server in notify_servers:
        server.async_stop_server.assert_awaited_once()


async def test_the_store_is_shared(hass: HomeAssistant) -> None:
    """Test every config entry acquires from the same store."""
    assert async_get_notify_servers(hass) is async_get_notify_servers(hass)
