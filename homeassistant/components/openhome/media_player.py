"""Support for Openhome Devices."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timedelta
import functools
import logging
from typing import Any, Concatenate, override

from openhomedevice.exceptions import OpenhomeError

from homeassistant.components import media_source, ssdp
from homeassistant.components.media_player import (
    SERVICE_PLAY_MEDIA,
    SERVICE_SELECT_SOURCE,
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.const import (
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    SERVICE_MEDIA_STOP,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_VOLUME_DOWN,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from . import OpenhomeConfigEntry
from .const import DOMAIN
from .services import SERVICE_INVOKE_PIN

SUPPORT_OPENHOME = (
    MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.TURN_ON
)

_LOGGER = logging.getLogger(__name__)

# Renewing is the only thing a device answers differently once it has
# forgotten a subscription, so it is both how the subscription is kept and
# how its loss is noticed. Often enough that a device which has gone is not
# left looking fine for long, and never so late that the lease runs out.
RENEWAL_INTERVAL = timedelta(minutes=5)


def _bootid(info: SsdpServiceInfo) -> int | None:
    """Return the boot id the announcement carries, if it has one.

    A device changes its boot id when it restarts. Reporting one at all is
    optional, so an announcement without one says nothing either way.
    """
    try:
        return int(info.ssdp_headers[ssdp.ATTR_SSDP_BOOTID], 10)
    except KeyError, ValueError:
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: OpenhomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Openhome config entry."""

    _LOGGER.debug("Setting up config entry: %s", config_entry.unique_id)

    device = config_entry.runtime_data.device

    entity = OpenhomeDevice(device)

    async_add_entities([entity])


type _FuncType[_T, **_P, _R] = Callable[Concatenate[_T, _P], Awaitable[_R]]
type _ReturnFuncType[_T, **_P, _R] = Callable[
    Concatenate[_T, _P], Coroutine[Any, Any, _R]
]


def catch_request_errors[_OpenhomeDeviceT: OpenhomeDevice, **_P, _R](
    action: str,
) -> Callable[
    [_FuncType[_OpenhomeDeviceT, _P, _R]], _ReturnFuncType[_OpenhomeDeviceT, _P, _R]
]:
    """Return decorator that catches errors and raises HomeAssistantError."""

    def call_wrapper(
        func: _FuncType[_OpenhomeDeviceT, _P, _R],
    ) -> _ReturnFuncType[_OpenhomeDeviceT, _P, _R]:
        """Call wrapper for decorator."""

        @functools.wraps(func)
        async def wrapper(
            self: _OpenhomeDeviceT, *args: _P.args, **kwargs: _P.kwargs
        ) -> _R:
            """Catch OpenhomeError errors."""
            try:
                return await func(self, *args, **kwargs)
            except OpenhomeError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=action,
                ) from err

        return wrapper

    return call_wrapper


class OpenhomeDevice(MediaPlayerEntity):
    """Representation of an Openhome device."""

    _attr_supported_features = SUPPORT_OPENHOME
    _attr_state = MediaPlayerState.PLAYING
    _attr_available = True

    def __init__(self, device):
        """Initialise the Openhome device."""
        self._device = device
        self._attr_unique_id = device.uuid()
        self._source_index: dict[str, int] = {}
        self._source_type: str | None = None
        self._in_standby: bool | None = None
        self._transport_state: str | None = None
        self._bootid: int | None = None
        self._lease = RENEWAL_INTERVAL
        self._cancel_renewal: CALLBACK_TYPE | None = None
        self._connection_lock = asyncio.Lock()
        self._attr_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, device.uuid()),
            },
            manufacturer=device.manufacturer(),
            model=device.model_name(),
            name=device.friendly_name(),
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to the device, and to what it announces about itself."""
        self.async_on_remove(self._async_cancel_renewal)
        await self._async_subscribe()

        # Registering replays what the device has already announced, so the
        # callback runs before this returns.
        self.async_on_remove(
            await ssdp.async_register_callback(
                self.hass, self._async_ssdp_change, {"_udn": self._device.uuid()}
            )
        )

    async def _async_subscribe(self) -> None:
        """Subscribe where the device can report its own changes."""
        if not self._device.events_enabled:
            return

        try:
            self._lease = await self._device.subscribe(self._handle_event)
        except OpenhomeError as err:
            # Polling still works, so a device that refuses is not an error.
            _LOGGER.debug(
                "Could not subscribe to %s, polling it instead: %s",
                self.entity_id,
                err,
            )
            return

        self._attr_should_poll = False
        self._async_schedule_renewal()

    @callback
    def _async_schedule_renewal(self) -> None:
        """Come back before the lease the device granted runs out."""
        self._async_cancel_renewal()

        if self.should_poll:
            # A polled device holds no subscription to keep.
            return

        # Half a lease, so a device granting less than we asked for is still
        # renewed in good time. Nothing renews on our behalf.
        delay = min(RENEWAL_INTERVAL, self._lease / 2)
        self._cancel_renewal = async_call_later(self.hass, delay, self._async_renew)

    @callback
    def _async_cancel_renewal(self) -> None:
        """Drop any renewal that has not come round yet."""
        if self._cancel_renewal is not None:
            self._cancel_renewal()
            self._cancel_renewal = None

    async def _async_renew(self, now: datetime) -> None:
        """Keep the subscription, and notice when the device has dropped it.

        A device that restarted, or that gave up on an event it could not
        deliver, announces neither: it answers every other request exactly
        as before and simply stops sending events. Renewing is the one
        request it answers differently.
        """
        async with self._connection_lock:
            self._cancel_renewal = None

            if not self._device.is_subscribed:
                await self._async_reconnect()
            else:
                try:
                    self._lease = await self._device.renew()
                except OpenhomeError as err:
                    _LOGGER.debug(
                        "%s no longer holds its subscription: %s", self.entity_id, err
                    )
                    # Left unsubscribed, so the next renewal picks it back up.
                    await self._async_device_gone()

        # However that went. A device that could not be reached this time is
        # only found again by coming round to look for it once more.
        self._async_schedule_renewal()

    async def _async_ssdp_change(
        self, info: SsdpServiceInfo, change: ssdp.SsdpChange
    ) -> None:
        """React to the device announcing itself, or going away.

        A device says so the moment it happens, where the activity check
        only comes round to noticing minutes later.
        """
        if change is ssdp.SsdpChange.UPDATE:
            # Only says the boot id is about to change. The announcement
            # that follows is the one to act on.
            return

        bootid = _bootid(info)

        # A device announces every service it offers, and reconnecting takes
        # long enough for the rest of that burst to arrive mid-way.
        async with self._connection_lock:
            if change is ssdp.SsdpChange.BYEBYE:
                _LOGGER.debug("%s said goodbye", self.entity_id)
                await self._async_device_gone()
                # A device that restarts says goodbye and announces itself in
                # one breath, and the two reach us in either order, so take
                # the goodbye as a prompt to look rather than as the answer.
                await self._async_reconnect()
                return

            # A restart is worth acting on whether or not its goodbye
            # arrived.
            rebooted = (
                bootid is not None
                and self._bootid is not None
                and bootid != self._bootid
            )
            if bootid is not None:
                self._bootid = bootid

            if not rebooted and self.available:
                # It is only repeating itself, and we are already talking to it.
                return

            # It has just come up, so it has forgotten what it granted us.
            if self._device.is_subscribed:
                await self._device.unsubscribe()

            _LOGGER.debug("%s announced itself, resubscribing", self.entity_id)
            await self._async_reconnect()

    async def _async_device_gone(self) -> None:
        """Report the device as unavailable, then release the subscription.

        In that order: releasing one the device is no longer there to release
        takes a timeout and retries for every service it offered.
        """
        self._attr_available = False
        self.async_write_ha_state()

        await self._device.unsubscribe()

    async def _async_reconnect(self) -> None:
        """Pick the device back up after it announced itself."""
        try:
            # The device description is cached, and the service URLs come
            # from it: one that restarted may not describe itself the same.
            await self._device.init()
        except OpenhomeError as err:
            _LOGGER.debug(
                "%s announced itself but could not be reached: %s",
                self.entity_id,
                err,
            )
            return

        await self._async_subscribe()

        # Being available again is also what makes the rest of the
        # announcements in the same burst do nothing.
        self._attr_available = True
        self.async_write_ha_state()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop receiving events."""
        await self._device.unsubscribe()

    @callback
    def _handle_event(self, changes: dict[str, Any]) -> None:
        """Apply what the device reported and publish the new state."""
        if changes.get("is_subscribed") is False:
            # Nothing further arrives until we resubscribe, which the next
            # activity check does once the device can be reached again.
            _LOGGER.warning("Lost the event subscription to %s", self.entity_id)
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._apply(changes)
        self._attr_available = True
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Update state of device."""
        try:
            changes: dict[str, Any] = {
                "room": await self._device.room(),
                "track_info": await self._device.track_info(),
                "sources": await self._device.sources(),
                "source": await self._device.source(),
                "is_in_standby": await self._device.is_in_standby(),
                "transport_state": await self._device.transport_state(),
            }

            if self._device.volume_enabled:
                changes["volume"] = await self._device.volume()
                changes["is_muted"] = await self._device.is_muted()

            self._apply(changes)
            self._attr_available = True
        except OpenhomeError as err:
            if self._attr_available:
                _LOGGER.warning("Error updating %s: %s", self.entity_id, err)
            self._attr_available = False

    def _apply(self, changes: dict[str, Any]) -> None:
        """Apply reported values, whichever of them the device sent."""
        if "room" in changes:
            self._attr_name = changes["room"]

        if "track_info" in changes:
            track_info = changes["track_info"]
            self._attr_media_image_url = track_info.get("albumArtwork")
            self._attr_media_album_name = track_info.get("albumTitle")
            self._attr_media_title = track_info.get("title")
            if artists := track_info.get("artist"):
                self._attr_media_artist = artists[0]
            self._attr_media_content_id = track_info.get("uri")
            self._attr_media_content_type = MediaType.MUSIC

        if "volume" in changes:
            self._attr_volume_level = changes["volume"] / 100.0

        if "is_muted" in changes:
            self._attr_is_volume_muted = changes["is_muted"]

        if "sources" in changes:
            sources = changes["sources"]
            self._source_index = {source["name"]: source["index"] for source in sources}
            self._attr_source_list = [source["name"] for source in sources]

        if "source" in changes:
            self._attr_source = changes["source"].get("name")
            self._source_type = changes["source"].get("type")

        if "is_in_standby" in changes:
            self._in_standby = changes["is_in_standby"]

        if "transport_state" in changes:
            self._transport_state = changes["transport_state"]

        self._attr_supported_features = self._supported_features()
        self._attr_state = self._state()

    def _supported_features(self) -> MediaPlayerEntityFeature:
        """Return what the device offers on the source it is playing."""
        features = SUPPORT_OPENHOME

        if self._device.volume_enabled:
            features |= (
                MediaPlayerEntityFeature.VOLUME_STEP
                | MediaPlayerEntityFeature.VOLUME_MUTE
                | MediaPlayerEntityFeature.VOLUME_SET
            )

        if self._source_type in ("Radio", "Receiver"):
            features |= (
                MediaPlayerEntityFeature.STOP
                | MediaPlayerEntityFeature.PLAY
                | MediaPlayerEntityFeature.PLAY_MEDIA
                | MediaPlayerEntityFeature.BROWSE_MEDIA
            )

        if self._source_type in ("Playlist", "Spotify"):
            features |= (
                MediaPlayerEntityFeature.PREVIOUS_TRACK
                | MediaPlayerEntityFeature.NEXT_TRACK
                | MediaPlayerEntityFeature.PAUSE
                | MediaPlayerEntityFeature.PLAY
                | MediaPlayerEntityFeature.PLAY_MEDIA
                | MediaPlayerEntityFeature.BROWSE_MEDIA
            )

        return features

    def _state(self) -> MediaPlayerState:
        """Return the state the reported standby and transport describe."""
        if self._in_standby:
            return MediaPlayerState.OFF
        if self._transport_state == "Paused":
            return MediaPlayerState.PAUSED
        if self._transport_state in ("Playing", "Buffering"):
            return MediaPlayerState.PLAYING
        if self._transport_state == "Stopped":
            return MediaPlayerState.IDLE
        # Device is playing an external source with no transport controls
        return MediaPlayerState.PLAYING

    @catch_request_errors(SERVICE_TURN_ON)
    @override
    async def async_turn_on(self) -> None:
        """Bring device out of standby."""
        await self._device.set_standby(False)

    @catch_request_errors(SERVICE_TURN_OFF)
    @override
    async def async_turn_off(self) -> None:
        """Put device in standby."""
        await self._device.set_standby(True)

    @catch_request_errors(SERVICE_PLAY_MEDIA)
    @override
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Send the play_media command to the media player."""
        if media_source.is_media_source_id(media_id):
            media_type = MediaType.MUSIC
            play_item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = play_item.url

        if media_type != MediaType.MUSIC:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_media_type",
                translation_placeholders={"media_type": str(media_type)},
            )

        media_id = async_process_play_media_url(self.hass, media_id)

        track_details = {"title": "Home Assistant", "uri": media_id}
        await self._device.play_media(track_details)

    @catch_request_errors(SERVICE_MEDIA_PAUSE)
    @override
    async def async_media_pause(self) -> None:
        """Send pause command."""
        await self._device.pause()

    @catch_request_errors(SERVICE_MEDIA_STOP)
    @override
    async def async_media_stop(self) -> None:
        """Send stop command."""
        await self._device.stop()

    @catch_request_errors(SERVICE_MEDIA_PLAY)
    @override
    async def async_media_play(self) -> None:
        """Send play command."""
        await self._device.play()

    @catch_request_errors(SERVICE_MEDIA_NEXT_TRACK)
    @override
    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self._device.skip(1)

    @catch_request_errors(SERVICE_MEDIA_PREVIOUS_TRACK)
    @override
    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await self._device.skip(-1)

    @catch_request_errors(SERVICE_SELECT_SOURCE)
    @override
    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        await self._device.set_source(self._source_index[source])

    @catch_request_errors(SERVICE_INVOKE_PIN)
    async def async_invoke_pin(self, pin):
        """Invoke pin."""
        if not self._device.pins_enabled:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="pins_not_supported",
            )
        await self._device.invoke_pin(pin)

    @catch_request_errors(SERVICE_VOLUME_UP)
    @override
    async def async_volume_up(self) -> None:
        """Volume up media player."""
        await self._device.increase_volume()

    @catch_request_errors(SERVICE_VOLUME_DOWN)
    @override
    async def async_volume_down(self) -> None:
        """Volume down media player."""
        await self._device.decrease_volume()

    @catch_request_errors(SERVICE_VOLUME_SET)
    @override
    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        await self._device.set_volume(int(volume * 100))

    @catch_request_errors(SERVICE_VOLUME_MUTE)
    @override
    async def async_mute_volume(self, mute: bool) -> None:
        """Mute (true) or unmute (false) media player."""
        await self._device.set_mute(mute)

    @override
    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Implement the websocket media browsing helper."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )
