"""The openhome component."""

from dataclasses import dataclass
import logging

from openhomedevice.device import Device
from openhomedevice.exceptions import OpenhomeError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .data import async_get_notify_servers
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class OpenhomeRuntimeData:
    """Runtime data for a configured device."""

    device: Device
    notify_address: str


type OpenhomeConfigEntry = ConfigEntry[OpenhomeRuntimeData]

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
PLATFORMS = [Platform.MEDIA_PLAYER, Platform.UPDATE]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the component."""
    async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: OpenhomeConfigEntry,
) -> bool:
    """Set up the configuration config entry."""
    _LOGGER.debug("Setting up config entry: %s", config_entry.unique_id)

    location = config_entry.data[CONF_HOST]
    notify_servers = async_get_notify_servers(hass)
    notify_address, event_handler = await notify_servers.async_acquire(location)

    device = Device(
        location,
        session=async_get_clientsession(hass),
        event_handler=event_handler,
    )

    try:
        await device.init()
    except OpenhomeError as exc:
        await notify_servers.async_release(notify_address)
        raise ConfigEntryNotReady from exc

    _LOGGER.debug("Initialised device: %s", device.uuid())

    config_entry.runtime_data = OpenhomeRuntimeData(device, notify_address)

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, config_entry: OpenhomeConfigEntry
) -> bool:
    """Cleanup before removing config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)

    if unloaded:
        await async_get_notify_servers(hass).async_release(
            config_entry.runtime_data.notify_address
        )

    return unloaded
