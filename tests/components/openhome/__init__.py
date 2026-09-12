"""Tests for the Linn / OpenHome integration."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry, async_fire_time_changed


async def setup_integration(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Set up the openhome integration."""
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def async_advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int
) -> None:
    """Let time pass, so anything due in that window comes round.

    Renewal is measured against the monotonic clock, which only the freezer
    moves.
    """
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    # Renewal runs as a background task, which is not waited on by default.
    await hass.async_block_till_done(wait_background_tasks=True)


async def async_poll(hass: HomeAssistant) -> None:
    """Trigger a poll of the polling platforms.

    Well past the media player's ten second scan interval.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
