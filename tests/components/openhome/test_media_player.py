"""Tests for the Openhome media player platform."""

from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from openhomedevice.device import Device
from openhomedevice.exceptions import OpenhomeConnectionError
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components import ssdp
from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_PLAY_MEDIA,
    SERVICE_SELECT_SOURCE,
    MediaType,
)
from homeassistant.components.openhome.const import DOMAIN
from homeassistant.components.openhome.services import (
    ATTR_PIN_INDEX,
    SERVICE_INVOKE_PIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
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
    STATE_IDLE,
    STATE_OFF,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from . import async_poll, setup_integration
from .conftest import HOST, TRACK_INFO

from tests.common import MockConfigEntry, snapshot_platform

ENTITY_ID = "media_player.friendly_name"
MEDIA_ID = "http://localhost/track.flac"

# Each action, the coroutine it drives, a source type that exposes the supported
# feature it is gated behind, and the arguments the device must be called with.
ACTIONS = [
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_TURN_ON,
        {},
        Device.set_standby,
        "Playlist",
        (False,),
        id="turn_on",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_TURN_OFF,
        {},
        Device.set_standby,
        "Playlist",
        (True,),
        id="turn_off",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_PLAY,
        {},
        Device.play,
        "Playlist",
        (),
        id="media_play",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_PAUSE,
        {},
        Device.pause,
        "Playlist",
        (),
        id="media_pause",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_STOP,
        {},
        Device.stop,
        "Radio",
        (),
        id="media_stop",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_NEXT_TRACK,
        {},
        Device.skip,
        "Playlist",
        (1,),
        id="media_next_track",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_PREVIOUS_TRACK,
        {},
        Device.skip,
        "Playlist",
        (-1,),
        id="media_previous_track",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_UP,
        {},
        Device.increase_volume,
        "Playlist",
        (),
        id="volume_up",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_DOWN,
        {},
        Device.decrease_volume,
        "Playlist",
        (),
        id="volume_down",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_SET,
        {ATTR_MEDIA_VOLUME_LEVEL: 0.5},
        Device.set_volume,
        "Playlist",
        (50,),
        id="volume_set",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_MUTE,
        {ATTR_MEDIA_VOLUME_MUTED: True},
        Device.set_mute,
        "Playlist",
        (True,),
        id="volume_mute",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_MUTE,
        {ATTR_MEDIA_VOLUME_MUTED: False},
        Device.set_mute,
        "Playlist",
        (False,),
        id="volume_unmute",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_SELECT_SOURCE,
        {ATTR_INPUT_SOURCE: "Radio"},
        Device.set_source,
        "Playlist",
        (1,),
        id="select_source",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
            ATTR_MEDIA_CONTENT_ID: MEDIA_ID,
        },
        Device.play_media,
        "Playlist",
        ({"title": "Home Assistant", "uri": MEDIA_ID},),
        id="play_media",
    ),
]


@pytest.fixture(autouse=True)
def media_proxy_token() -> Generator[None]:
    """Freeze the media proxy token, which otherwise varies per run."""
    with patch("secrets.token_hex", return_value="mock_token"):
        yield


@pytest.fixture
def platforms() -> list[Platform]:
    """Only load the media player platform."""
    return [Platform.MEDIA_PLAYER]


async def setup_media_player(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Load the media player platform and poll once."""
    await setup_integration(hass, mock_config_entry)

    # Supported features are only set once the device has been polled.
    await async_poll(hass)


@pytest.mark.parametrize(
    ("domain", "service", "data", "method", "source_type", "expected_args"), ACTIONS
)
async def test_action_calls_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    domain: str,
    service: str,
    data: dict[str, Any],
    method: Callable[..., Any],
    source_type: str,
    expected_args: tuple[Any, ...],
) -> None:
    """Test every action calls the device with the arguments it expects."""
    mock_device.source.return_value = {
        "index": 0,
        "name": source_type,
        "type": source_type,
    }

    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
    )

    getattr(mock_device, method.__name__).assert_awaited_once_with(*expected_args)


@pytest.mark.parametrize(
    ("domain", "service", "data", "method", "source_type", "expected_args"), ACTIONS
)
async def test_action_error_is_raised(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    domain: str,
    service: str,
    data: dict[str, Any],
    method: Callable[..., Any],
    source_type: str,
    expected_args: tuple[Any, ...],
) -> None:
    """Test every action raises when the device rejects the request."""
    # The feature each action is gated behind depends on the selected source.
    mock_device.source.return_value = {
        "index": 0,
        "name": source_type,
        "type": source_type,
    }

    await setup_media_player(hass, mock_config_entry)

    mocked = getattr(mock_device, method.__name__)
    mocked.side_effect = OpenhomeConnectionError("no route to host")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            domain, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
        )

    # The message is keyed on the service name, so each action reports its own.
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == service
    mocked.assert_awaited()


async def test_invoke_pin_calls_device(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_device: MagicMock
) -> None:
    """Test the invoke_pin action passes the requested pin to the device."""
    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_INVOKE_PIN,
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_PIN_INDEX: 3},
        blocking=True,
    )

    mock_device.invoke_pin.assert_awaited_once_with(3)


@pytest.mark.parametrize(
    ("pins_enabled", "translation_key", "await_count"),
    [
        pytest.param(True, SERVICE_INVOKE_PIN, 1, id="pins_supported"),
        pytest.param(False, "pins_not_supported", 0, id="pins_not_supported"),
    ],
)
async def test_invoke_pin(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    pins_enabled: bool,
    translation_key: str,
    await_count: int,
) -> None:
    """Test invoking a pin on a device with and without pin support."""
    mock_device.pins_enabled = pins_enabled

    await setup_media_player(hass, mock_config_entry)
    mock_device.invoke_pin.side_effect = OpenhomeConnectionError("no route to host")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_INVOKE_PIN,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_PIN_INDEX: 1},
            blocking=True,
        )

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == translation_key
    assert mock_device.invoke_pin.await_count == await_count


async def test_media_player(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Test the media player is set up from the device state."""
    mock_device.track_info.return_value = TRACK_INFO

    await setup_media_player(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_becomes_unavailable_and_recovers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a failed poll logs once, marks the device unavailable and recovers."""
    await setup_media_player(hass, mock_config_entry)

    assert hass.states.get(ENTITY_ID).state == STATE_PLAYING

    mock_device.room.side_effect = OpenhomeConnectionError("device unreachable")
    await async_poll(hass)

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert caplog.text.count("device unreachable") == 1

    # A second consecutive failure must not log the same outage again.
    await async_poll(hass)

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert caplog.text.count("device unreachable") == 1

    mock_device.room.side_effect = None
    await async_poll(hass)

    assert hass.states.get(ENTITY_ID).state == STATE_PLAYING


@pytest.mark.parametrize(
    ("in_standby", "transport_state", "expected_state"),
    [
        pytest.param(True, "Playing", STATE_OFF, id="standby"),
        pytest.param(False, "Paused", STATE_PAUSED, id="paused"),
        pytest.param(False, "Playing", STATE_PLAYING, id="playing"),
        pytest.param(False, "Buffering", STATE_PLAYING, id="buffering"),
        pytest.param(False, "Stopped", STATE_IDLE, id="stopped"),
        # An external source with no transport controls still counts as playing.
        pytest.param(False, "Unknown", STATE_PLAYING, id="external_source"),
    ],
)
async def test_state_mapping(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    in_standby: bool,
    transport_state: str,
    expected_state: str,
) -> None:
    """Test the device transport state maps onto the media player state."""
    mock_device.is_in_standby.return_value = in_standby
    mock_device.transport_state.return_value = transport_state

    await setup_media_player(hass, mock_config_entry)

    assert hass.states.get(ENTITY_ID).state == expected_state


@pytest.fixture
def subscribed_device(mock_device: MagicMock) -> MagicMock:
    """Return a device that reports its own changes instead of being polled."""
    mock_device.events_enabled = True
    return mock_device


def emitted_callback(mock_device: MagicMock) -> Callable[..., Any]:
    """Return the callback the entity handed to subscribe()."""
    mock_device.subscribe.assert_awaited_once()
    return mock_device.subscribe.await_args.args[0]


async def test_subscribes_and_stops_polling(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
) -> None:
    """Test a device that reports changes is subscribed to, not polled."""
    await setup_integration(hass, mock_config_entry)

    subscribed_device.subscribe.assert_awaited_once()

    subscribed_device.room.reset_mock()
    await async_poll(hass)

    subscribed_device.room.assert_not_awaited()


async def test_polls_when_the_device_cannot_report_changes(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_device: MagicMock
) -> None:
    """Test a device without the services for events is polled."""
    mock_device.events_enabled = False

    await setup_media_player(hass, mock_config_entry)

    mock_device.subscribe.assert_not_awaited()
    mock_device.room.assert_awaited()


async def test_failed_subscription_falls_back_to_polling(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
) -> None:
    """Test the device is still polled when subscribing fails."""
    subscribed_device.subscribe.side_effect = OpenhomeConnectionError("refused")

    await setup_media_player(hass, mock_config_entry)

    subscribed_device.room.assert_awaited()


async def test_event_updates_the_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
) -> None:
    """Test a reported change is published without polling the device."""
    await setup_integration(hass, mock_config_entry)
    handle = emitted_callback(subscribed_device)

    handle({"is_in_standby": False, "transport_state": "Paused"})
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == STATE_PAUSED

    handle({"transport_state": "Stopped"})
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == STATE_IDLE


async def test_event_applies_only_what_changed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
) -> None:
    """Test a later event does not discard what an earlier one reported."""
    await setup_integration(hass, mock_config_entry)
    handle = emitted_callback(subscribed_device)

    handle({"is_in_standby": False, "transport_state": "Playing", "volume": 30})
    await hass.async_block_till_done()

    handle({"volume": 70})
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_MEDIA_VOLUME_LEVEL] == 0.7
    # The standby and transport from the first event still decide the state.
    assert state.state == STATE_PLAYING


async def test_lost_subscription_marks_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test losing the subscription marks the device unavailable."""
    await setup_integration(hass, mock_config_entry)
    handle = emitted_callback(subscribed_device)

    handle({"is_in_standby": False, "transport_state": "Playing"})
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_PLAYING

    handle({"is_subscribed": False})
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert "Lost the event subscription" in caplog.text


async def test_unsubscribes_on_removal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
) -> None:
    """Test the subscription is released when the entry is unloaded."""
    await setup_integration(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    subscribed_device.unsubscribe.assert_awaited_once()


def announcement(bootid: int | None = None) -> SsdpServiceInfo:
    """Return an SSDP announcement, optionally carrying a boot id."""
    headers = {} if bootid is None else {ssdp.ATTR_SSDP_BOOTID: str(bootid)}
    return SsdpServiceInfo(
        ssdp_usn="uuid:uuid",
        ssdp_st="urn:av-openhome-org:service:Product:1",
        ssdp_location=HOST,
        ssdp_headers=headers,
        upnp={},
    )


def ssdp_callback(mock_ssdp_register: AsyncMock) -> Callable[..., Any]:
    """Return the SSDP callback the entity registered."""
    mock_ssdp_register.assert_awaited_once()
    return mock_ssdp_register.await_args.args[1]


async def test_registers_for_its_own_announcements(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
    mock_ssdp_register: AsyncMock,
) -> None:
    """Test the entity listens for announcements from its own device."""
    await setup_integration(hass, mock_config_entry)

    mock_ssdp_register.assert_awaited_once()
    assert mock_ssdp_register.await_args.args[2] == {"_udn": "uuid"}


async def test_byebye_marks_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
    mock_ssdp_register: AsyncMock,
) -> None:
    """Test a device going away releases the subscription."""
    await setup_integration(hass, mock_config_entry)
    handle_ssdp = ssdp_callback(mock_ssdp_register)

    emitted_callback(subscribed_device)({"is_in_standby": False})
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state != STATE_UNAVAILABLE

    await handle_ssdp(announcement(), ssdp.SsdpChange.BYEBYE)
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    subscribed_device.unsubscribe.assert_awaited()


async def test_alive_resubscribes_after_byebye(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
    mock_ssdp_register: AsyncMock,
) -> None:
    """Test the device is picked back up when it announces itself again."""
    await setup_integration(hass, mock_config_entry)
    handle_ssdp = ssdp_callback(mock_ssdp_register)

    subscribed_device.is_subscribed = False
    await handle_ssdp(announcement(), ssdp.SsdpChange.BYEBYE)
    await hass.async_block_till_done()

    subscribed_device.init.reset_mock()
    subscribed_device.subscribe.reset_mock()

    await handle_ssdp(announcement(), ssdp.SsdpChange.ALIVE)
    await hass.async_block_till_done()

    # Re-read the description: a device that restarted may have moved.
    subscribed_device.init.assert_awaited_once()
    subscribed_device.subscribe.assert_awaited_once()


async def test_alive_while_subscribed_is_left_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
    mock_ssdp_register: AsyncMock,
) -> None:
    """Test a routine announcement does not disturb a live subscription."""
    await setup_integration(hass, mock_config_entry)
    handle_ssdp = ssdp_callback(mock_ssdp_register)

    subscribed_device.is_subscribed = True
    subscribed_device.subscribe.reset_mock()

    await handle_ssdp(announcement(bootid=1), ssdp.SsdpChange.ALIVE)
    await handle_ssdp(announcement(bootid=1), ssdp.SsdpChange.ALIVE)
    await hass.async_block_till_done()

    subscribed_device.subscribe.assert_not_awaited()


async def test_reboot_resubscribes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
    mock_ssdp_register: AsyncMock,
) -> None:
    """Test a changed boot id resubscribes, even without a goodbye."""
    await setup_integration(hass, mock_config_entry)
    handle_ssdp = ssdp_callback(mock_ssdp_register)

    subscribed_device.is_subscribed = True
    await handle_ssdp(announcement(bootid=1), ssdp.SsdpChange.ALIVE)
    await hass.async_block_till_done()

    subscribed_device.subscribe.reset_mock()
    # The device restarted and has forgotten the subscription it granted.
    subscribed_device.unsubscribe.side_effect = lambda: setattr(
        subscribed_device, "is_subscribed", False
    )

    await handle_ssdp(announcement(bootid=2), ssdp.SsdpChange.ALIVE)
    await hass.async_block_till_done()

    subscribed_device.unsubscribe.assert_awaited()
    subscribed_device.subscribe.assert_awaited_once()


async def test_update_announcement_is_ignored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    subscribed_device: MagicMock,
    mock_ssdp_register: AsyncMock,
) -> None:
    """Test the warning of a coming boot id change is left for the alive."""
    await setup_integration(hass, mock_config_entry)
    handle_ssdp = ssdp_callback(mock_ssdp_register)

    subscribed_device.is_subscribed = True
    await handle_ssdp(announcement(bootid=1), ssdp.SsdpChange.ALIVE)
    await hass.async_block_till_done()

    subscribed_device.unsubscribe.reset_mock()
    subscribed_device.subscribe.reset_mock()

    # Carries the boot id the device is moving to, so acting on it here would
    # tear the subscription down while the device is still serving it.
    await handle_ssdp(announcement(bootid=2), ssdp.SsdpChange.UPDATE)
    await hass.async_block_till_done()

    subscribed_device.unsubscribe.assert_not_awaited()
    subscribed_device.subscribe.assert_not_awaited()
