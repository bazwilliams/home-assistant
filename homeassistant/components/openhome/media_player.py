"""Support for Openhome Devices."""

from collections.abc import Awaitable, Callable, Coroutine
import functools
import logging
from typing import Any, Concatenate, override

from openhomedevice.exceptions import OpenhomeError

from homeassistant.components import media_source
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenhomeConfigEntry
from .const import DOMAIN
from .services import SERVICE_INVOKE_PIN

SUPPORT_OPENHOME = (
    MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.TURN_ON
)

_LOGGER = logging.getLogger(__name__)


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
        """Subscribe to the device, where it can report its own changes."""
        if not self._device.events_enabled:
            return

        try:
            await self._device.subscribe(self._handle_event)
        except OpenhomeError as err:
            # Polling still works, so a device that refuses is not an error.
            _LOGGER.debug(
                "Could not subscribe to %s, polling it instead: %s",
                self.entity_id,
                err,
            )
            return

        self._attr_should_poll = False

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop receiving events."""
        await self._device.unsubscribe()

    @callback
    def _handle_event(self, changes: dict[str, Any]) -> None:
        """Apply what the device reported and publish the new state."""
        if changes.get("is_subscribed") is False:
            # Renewal failed. Nothing further arrives until we resubscribe.
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
            _LOGGER.error(
                "Invalid media type %s. Only %s is supported",
                media_type,
                MediaType.MUSIC,
            )
            return

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
