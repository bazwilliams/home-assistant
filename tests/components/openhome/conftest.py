"""Fixtures for the Openhome integration tests."""

import asyncio
from collections.abc import Generator
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from openhomedevice.device import Device
from openhomedevice.exceptions import OpenhomeDeviceError
import pytest

from homeassistant.components.openhome.const import DOMAIN
from homeassistant.components.ssdp import ATTR_SSDP_BOOTID, SsdpChange
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from tests.common import MockConfigEntry

HOST = "http://localhost"
NOTIFY_ADDRESS = "192.0.2.1"

# What a Linn grants, which is the most it will: the device caps whatever
# it is asked for at its own maximum.
LEASE = timedelta(minutes=30)

# Registering an SSDP callback replays every service the device has already
# advertised to it.
SSDP_SERVICE_TYPES = (
    "upnp:rootdevice",
    "urn:av-openhome-org:service:Product:1",
    "urn:av-openhome-org:service:Volume:1",
    "urn:av-openhome-org:service:Transport:1",
    "urn:av-openhome-org:service:Info:1",
)

TRACK_INFO = {
    "albumArtwork": "http://localhost/album.jpg",
    "albumTitle": "album_title",
    "artist": ["artist"],
    "title": "title",
    "uri": "http://localhost/track.flac",
}

SOURCES = [
    {"index": 0, "name": "Playlist", "type": "Playlist"},
    {"index": 1, "name": "Radio", "type": "Radio"},
]

# The device coroutines the actions drive, referenced from the library so a
# rename upstream fails the tests rather than leaving an action uncovered.
ACTION_METHODS = (
    Device.set_standby,
    Device.play,
    Device.pause,
    Device.stop,
    Device.skip,
    Device.increase_volume,
    Device.decrease_volume,
    Device.set_volume,
    Device.set_mute,
    Device.set_source,
    Device.play_media,
    Device.invoke_pin,
)


async def reached() -> None:
    """Answer as a call over the network does, by suspending first.

    Without this a caller runs to completion before another can start, and
    anything that races in reality never races here.
    """
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def mock_ssdp_component() -> Generator[None]:
    """Keep the ssdp dependency from listening on the network."""
    with (
        patch("homeassistant.components.ssdp.Scanner", autospec=True),
        patch("homeassistant.components.ssdp.Server", autospec=True),
    ):
        yield


def announcement(
    bootid: int | None = None, service_type: str = SSDP_SERVICE_TYPES[1]
) -> SsdpServiceInfo:
    """Return an SSDP announcement, optionally carrying a boot id."""
    headers = {} if bootid is None else {ATTR_SSDP_BOOTID: str(bootid)}
    return SsdpServiceInfo(
        ssdp_usn="uuid:uuid",
        ssdp_st=service_type,
        ssdp_location=HOST,
        ssdp_headers=headers,
        upnp={},
    )


@pytest.fixture(autouse=True)
def mock_ssdp_register(hass: HomeAssistant) -> Generator[AsyncMock]:
    """Capture the SSDP callback without needing the ssdp component.

    Registering replays what the device has already announced, once per
    service, as concurrent jobs. Reproduced here because a callback that
    reconnects has to cope with the whole burst.
    """

    async def register(_hass: HomeAssistant, callback, match_dict) -> MagicMock:
        for service_type in SSDP_SERVICE_TYPES:
            hass.async_create_task(
                callback(announcement(service_type=service_type), SsdpChange.ALIVE)
            )
        return MagicMock()

    mock_register = AsyncMock(side_effect=register)
    with patch(
        "homeassistant.components.openhome.media_player.ssdp.async_register_callback",
        mock_register,
    ):
        yield mock_register


@pytest.fixture(autouse=True)
def mock_notify_servers() -> Generator[MagicMock]:
    """Keep the shared notify server from binding a socket during tests."""
    servers = MagicMock()
    servers.async_acquire = AsyncMock(return_value=(NOTIFY_ADDRESS, MagicMock()))
    servers.async_release = AsyncMock()

    with patch(
        "homeassistant.components.openhome.async_get_notify_servers",
        return_value=servers,
    ):
        yield servers


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mocked config entry."""
    return MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id="uuid")


@pytest.fixture
def platforms() -> list[Platform]:
    """Platforms to load; override per module or per test."""
    return []


@pytest.fixture(autouse=True)
def patch_platforms(platforms: list[Platform]) -> Generator[None]:
    """Load only the platforms the test asks for."""
    with patch("homeassistant.components.openhome.PLATFORMS", platforms):
        yield


@pytest.fixture
def mock_device_class() -> Generator[MagicMock]:
    """Return the patched Openhome Device class."""
    with patch("homeassistant.components.openhome.Device", MagicMock()) as mock_class:
        device = mock_class.return_value
        device.init = AsyncMock(side_effect=reached)
        device.uuid = MagicMock(return_value="uuid")
        device.manufacturer = MagicMock(return_value="manufacturer")
        device.model_name = MagicMock(return_value="model_name")
        device.friendly_name = MagicMock(return_value="friendly_name")
        device.volume_enabled = True
        device.pins_enabled = True
        device.room = AsyncMock(return_value="room")
        device.track_info = AsyncMock(return_value={})
        device.volume = AsyncMock(return_value=50)
        device.is_muted = AsyncMock(return_value=False)
        device.sources = AsyncMock(return_value=SOURCES)
        device.source = AsyncMock(return_value=SOURCES[0])
        device.is_in_standby = AsyncMock(return_value=False)
        device.transport_state = AsyncMock(return_value="Playing")
        device.software_status = AsyncMock(return_value=None)
        # Polled by default; the tests that want events set this True.
        device.events_enabled = False
        # Only true once subscribe() has returned, as on a real device.
        # Announcements arrive during that window and race it.
        device.is_subscribed = False

        # Tests that care set device.lease to what the device grants.
        device.lease = LEASE

        async def subscribe(_callback) -> timedelta:
            await reached()
            device.is_subscribed = True
            return device.lease

        async def renew() -> timedelta:
            await reached()
            if not device.is_subscribed:
                raise OpenhomeDeviceError("not subscribed")
            return device.lease

        async def unsubscribe() -> None:
            await reached()
            device.is_subscribed = False

        device.subscribe = AsyncMock(side_effect=subscribe)
        device.renew = AsyncMock(side_effect=renew)
        device.unsubscribe = AsyncMock(side_effect=unsubscribe)
        device.update_firmware = AsyncMock()
        for method in ACTION_METHODS:
            setattr(device, method.__name__, AsyncMock())
        yield mock_class


@pytest.fixture
def mock_device(mock_device_class: MagicMock) -> MagicMock:
    """Return a mocked Openhome device that polls successfully."""
    return mock_device_class.return_value
