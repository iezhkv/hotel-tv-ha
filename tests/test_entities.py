"""Entities, push updates from the event stream, the stream loop, diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import copy
import json
from typing import Any
from unittest.mock import patch

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.hotel_tv.const import DOMAIN, EVENT_HOTEL_TV
from custom_components.hotel_tv.diagnostics import async_get_config_entry_diagnostics
from custom_components.hotel_tv.sse import SseEvent

from .conftest import (
    API,
    CH2,
    DEV101,
    PKG_DEFAULT,
    PKG_PREMIUM,
    R101,
    R102,
    REAL_RUN_EVENTS,
    TOKEN,
    calls,
    mock_server,
    room,
    room_device,
)

TV = "media_player.hotel_demo_101_tv"


async def _event(hass: HomeAssistant, entry, **event: Any) -> None:
    await entry.runtime_data.async_handle_event(event)
    await hass.async_block_till_done()


async def test_entities_created(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """A room with a TV gets all entities; a room without one only guest/occupancy."""
    await setup_entry()
    registry = er.async_get(hass)
    ids_101 = {
        e.entity_id
        for e in er.async_entries_for_device(registry, room_device(hass, R101).id)
    }
    assert ids_101 == {
        TV,
        "binary_sensor.hotel_demo_101_occupied",
        "binary_sensor.hotel_demo_101_tv_online",
        "sensor.hotel_demo_101_guest",
        "sensor.hotel_demo_101_current_channel",
        "button.hotel_demo_101_reload_tv",
        "button.hotel_demo_101_identify_tv",
        "select.hotel_demo_101_channel_package",
    }
    ids_102 = {
        e.entity_id
        for e in er.async_entries_for_device(registry, room_device(hass, R102).id)
    }
    assert ids_102 == {
        "binary_sensor.hotel_demo_102_occupied",
        "sensor.hotel_demo_102_guest",
        "select.hotel_demo_102_channel_package",
    }
    assert hass.states.get("button.hotel_demo_refresh_channels") is not None


async def test_states(hass: HomeAssistant, setup_entry: Callable[..., Any]) -> None:
    """Initial states from /rooms, /channels, /packages."""
    await setup_entry()
    tv = hass.states.get(TV)
    assert tv.state == STATE_ON
    assert tv.attributes["source"] == "1 · BNT 1"
    # The default package's channels, in package order.
    assert tv.attributes["source_list"] == ["2 · bTV", "1 · BNT 1"]
    assert tv.attributes["volume_level"] == 0.4
    assert tv.attributes["is_volume_muted"] is False
    assert tv.attributes["media_title"] == "BNT 1"
    assert tv.attributes["channel_number"] == 1
    assert hass.states.get("binary_sensor.hotel_demo_101_occupied").state == STATE_ON
    assert hass.states.get("binary_sensor.hotel_demo_102_occupied").state == STATE_OFF
    assert hass.states.get("binary_sensor.hotel_demo_101_tv_online").state == STATE_ON
    assert hass.states.get("sensor.hotel_demo_101_guest").state == "Maria Ivanova"
    assert hass.states.get("sensor.hotel_demo_102_guest").state == STATE_UNKNOWN
    assert hass.states.get("sensor.hotel_demo_101_current_channel").state == "BNT 1"
    # No own package = the default one; room 102 has its own.
    assert hass.states.get("select.hotel_demo_101_channel_package").state == "Standard"
    assert hass.states.get("select.hotel_demo_102_channel_package").state == "Premium"


async def test_push_updates(
    hass: HomeAssistant, setup_entry: Callable[..., Any], aioclient_mock
) -> None:
    """device.state / offline / online events update the entities without polling."""
    entry = await setup_entry()
    await _event(
        hass,
        entry,
        id=10,
        type="device.state",
        room_id=R101,
        device_id=DEV101,
        payload={
            "power": True,
            "channel_id": CH2,
            "channel_number": 2,
            "channel_name": "bTV",
            "volume": 15,
            "muted": True,
        },
    )
    tv = hass.states.get(TV)
    assert tv.attributes["source"] == "2 · bTV"
    assert tv.attributes["volume_level"] == 0.15
    assert tv.attributes["is_volume_muted"] is True
    assert hass.states.get("sensor.hotel_demo_101_current_channel").state == "bTV"

    await _event(hass, entry, type="device.offline", room_id=R101, device_id=DEV101)
    assert hass.states.get(TV).state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.hotel_demo_101_tv_online").state == STATE_OFF
    await _event(hass, entry, type="device.online", room_id=R101, device_id=DEV101)
    assert hass.states.get(TV).state == STATE_ON

    await _event(
        hass,
        entry,
        type="device.state",
        room_id=R101,
        device_id=DEV101,
        payload={"power": False},
    )
    assert hass.states.get(TV).state == STATE_OFF
    assert (
        hass.states.get("sensor.hotel_demo_101_current_channel").state == STATE_UNKNOWN
    )


async def test_room_updated_refetches(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """room.updated applies the payload and re-reads the room."""
    entry = await setup_entry()
    fresh = room(R102)
    fresh.update(occupied=True, guest_name="John Smith", guest_locale="en")
    aioclient_mock.clear_requests()
    mock_server(aioclient_mock, rooms=[room(R101), fresh])
    await _event(
        hass,
        entry,
        type="room.updated",
        room_id=R102,
        payload={
            "guest_name": "John Smith",
            "occupied": True,
            "package_id": PKG_PREMIUM,
        },
    )
    assert hass.states.get("sensor.hotel_demo_102_guest").state == "John Smith"
    guest = hass.states.get("sensor.hotel_demo_102_guest")
    assert guest.attributes["guest_locale"] == "en"
    assert calls(aioclient_mock, "get", f"{API}/rooms/{R102}") == [None]


async def test_new_tv_and_new_room(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A room that gets its first TV, and a new room, get their entities on refresh."""
    entry = await setup_entry()
    r102 = room(R102)
    r102["devices"] = [{**room(R101)["devices"][0], "id": "dev-102", "name": "TV 102"}]
    r103 = {**room(R102), "id": "room-103", "name": "103", "devices": []}
    aioclient_mock.clear_requests()
    mock_server(aioclient_mock, rooms=[room(R101), r102, r103])
    assert await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("media_player.hotel_demo_102_tv").state == STATE_ON
    assert hass.states.get("sensor.hotel_demo_103_guest") is not None


async def test_room_removed_and_server_down(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A room gone from the server loses its device; an unreachable server = unavailable."""
    entry = await setup_entry()
    aioclient_mock.clear_requests()
    mock_server(aioclient_mock, rooms=[room(R101)])
    assert await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    from homeassistant.helpers import device_registry as dr

    from custom_components.hotel_tv.hub import find_device

    assert find_device(dr.async_get(hass), entry.entry_id, R102) is None

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/rooms", exc=ClientError())
    aioclient_mock.get(f"{API}/channels", exc=ClientError())
    aioclient_mock.get(f"{API}/packages", exc=ClientError())
    assert not await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(TV).state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.hotel_demo_101_guest").state == STATE_UNAVAILABLE


async def test_channels_updated(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """channels.updated re-reads channels and packages."""
    entry = await setup_entry()
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{API}/channels",
        json={"channels": [{"id": CH2, "number": 7, "name": "bTV", "enabled": True}]},
    )
    aioclient_mock.get(
        f"{API}/packages",
        json={"packages": [{"id": PKG_DEFAULT, "name": "Basic", "is_default": True}]},
    )
    await _event(hass, entry, type="channels.updated", payload={})
    assert hass.states.get(TV).attributes["source_list"] == ["7 · bTV"]
    select = hass.states.get("select.hotel_demo_101_channel_package")
    assert select.attributes["options"] == ["Basic"]


async def test_bus_event(hass: HomeAssistant, setup_entry: Callable[..., Any]) -> None:
    """Every server event is fired as hotel_tv_event with the room's HA device id."""
    entry = await setup_entry()
    events = async_capture_events(hass, EVENT_HOTEL_TV)
    await entry.runtime_data.async_handle_sse(
        SseEvent(
            event="message.created",
            id="77",
            data=json.dumps(
                {
                    "id": 77,
                    "type": "message.created",
                    "room_id": R101,
                    "device_id": None,
                    "payload": {"message_id": "m1", "title": "Hi"},
                    "created_at": "2026-11-18T10:00:00Z",
                }
            ),
        )
    )
    await entry.runtime_data.async_handle_sse(
        SseEvent(event="ping", data="{}", id="77")
    )
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert data["type"] == "message.created"
    assert data["room_name"] == "101"
    assert data["device_id"] == room_device(hass, R101).id
    assert data["payload"] == {"message_id": "m1", "title": "Hi"}
    assert entry.runtime_data.last_event_id == "77"


async def test_media_player_commands(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Each media player action is one room command."""
    await setup_entry()
    url = f"{API}/rooms/{R101}/command"
    aioclient_mock.post(url, json={"ok": True, "devices": 1})
    target = {"entity_id": TV}
    for service, data in (
        ("turn_off", {}),
        ("turn_on", {}),
        ("select_source", {"source": "2 · bTV"}),
        ("select_source", {"source": "BNT 1"}),
        ("volume_set", {"volume_level": 0.25}),
        ("volume_up", {}),
        ("volume_down", {}),
        ("volume_mute", {"is_volume_muted": True}),
        ("volume_mute", {"is_volume_muted": False}),
        ("media_next_track", {}),
        ("media_previous_track", {}),
        ("media_play", {}),
        ("media_pause", {}),
    ):
        await hass.services.async_call(
            "media_player", service, {**target, **data}, blocking=True
        )
    assert calls(aioclient_mock, "post", url) == [
        {"type": "power_off"},
        {"type": "power_on"},
        {"type": "set_channel", "payload": {"channel_id": CH2}},
        {
            "type": "set_channel",
            "payload": {"channel_id": "33333333-3333-4333-8333-333333333001"},
        },
        {"type": "set_volume", "payload": {"volume": 25}},
        {"type": "volume_up"},
        {"type": "volume_down"},
        {"type": "mute"},
        {"type": "unmute"},
        {"type": "channel_up"},
        {"type": "channel_down"},
        {"type": "play"},
        {"type": "pause"},
    ]


async def test_buttons_and_select(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Reload / identify / refresh channels; the package select PATCHes the room."""
    await setup_entry()
    cmd = f"{API}/rooms/{R101}/command"
    aioclient_mock.post(cmd, json={"ok": True, "devices": 1})
    aioclient_mock.post(
        f"{API}/channels/refresh", json={"ok": True, "channels": 2, "started": True}
    )
    patched = room(R101)
    patched["package_id"] = PKG_PREMIUM
    aioclient_mock.patch(f"{API}/rooms/{R101}", json=patched)
    for entity_id in (
        "button.hotel_demo_101_reload_tv",
        "button.hotel_demo_101_identify_tv",
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.hotel_demo_refresh_channels"},
        blocking=True,
    )
    assert calls(aioclient_mock, "post", cmd) == [
        {"type": "reload"},
        {"type": "identify"},
    ]
    assert calls(aioclient_mock, "post", f"{API}/channels/refresh?wait=false") == [{}]

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.hotel_demo_101_channel_package", "option": "Premium"},
        blocking=True,
    )
    assert calls(aioclient_mock, "patch", f"{API}/rooms/{R101}") == [
        {"package_id": PKG_PREMIUM}
    ]
    await hass.async_block_till_done()
    assert hass.states.get("select.hotel_demo_101_channel_package").state == "Premium"
    # Back to the default package = follow the default (null).
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.hotel_demo_101_channel_package", "option": "Standard"},
        blocking=True,
    )
    assert calls(aioclient_mock, "patch", f"{API}/rooms/{R101}")[-1] == {
        "package_id": None
    }


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, setup_entry
) -> None:
    """A refused token at setup asks for a new one."""
    aioclient_mock.get(
        f"{API}/info",
        status=401,
        json={"error": {"code": "unauthorized", "message": ""}},
    )
    entry = await _setup_failing(hass, setup_entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def _setup_failing(hass: HomeAssistant, setup_entry) -> Any:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import ENTRY_DATA, PROPERTY

    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_not_ready(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, setup_entry
) -> None:
    """An unreachable server retries later."""
    aioclient_mock.get(f"{API}/info", exc=ClientError())
    entry = await _setup_failing(hass, setup_entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_event_stream_loop(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    no_event_stream,
) -> None:
    """The loop applies events, reconnects with Last-Event-ID and stops on unload."""
    entry = await setup_entry()
    hub = entry.runtime_data
    seen_headers: list[dict[str, str]] = []
    body = (
        "event: ping\ndata: {}\n\n"
        f'id: 5\nevent: device.state\ndata: {{"id": 5, "type": "device.state", '
        f'"room_id": "{R101}", "device_id": "{DEV101}", "payload": {{"volume": 70}}}}\n\n'
    )

    async def _events(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        seen_headers.append(aioclient_mock.mock_calls[-1][3])
        if len(seen_headers) == 1:
            return AiohttpClientMockResponse(method, url, text=body)
        hub._stopping = True
        hub._stop_event.set()
        raise ClientError("gone")

    aioclient_mock.get(f"{API}/events", side_effect=_events)
    bus = async_capture_events(hass, EVENT_HOTEL_TV)
    with patch("custom_components.hotel_tv.hub.SSE_BACKOFF_MIN", 0):
        await asyncio.wait_for(REAL_RUN_EVENTS(hub), 5)
    await hass.async_block_till_done()
    # Applied (then a full refresh after the stream ended re-read the rooms).
    assert [(e.data["type"], e.data["payload"]) for e in bus] == [
        ("device.state", {"volume": 70})
    ]
    assert hub.last_event_id == "5"
    assert len(calls(aioclient_mock, "get", f"{API}/rooms")) >= 2
    assert hub.stream_connected is False
    assert "Last-Event-ID" not in seen_headers[0]
    assert seen_headers[1]["Last-Event-ID"] == "5"
    assert seen_headers[1]["Authorization"] == f"Bearer {TOKEN}"


async def test_diagnostics(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """The token and guest data are redacted."""
    entry = await setup_entry()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"]["token"] == "**REDACTED**"
    assert TOKEN not in json.dumps(diag)
    assert "Maria" not in json.dumps(diag)
    assert diag["channels"] == 3


async def test_source_list_follows_package(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The room's own package limits the sources; unknown package = all enabled."""
    from homeassistant.exceptions import ServiceValidationError
    import pytest

    entry = await setup_entry()
    hub = entry.runtime_data
    aioclient_mock.post(f"{API}/rooms/{R101}/command", json={"ok": True, "devices": 1})

    # Own package with one channel: only that one, and others are refused.
    hub.packages.append(
        {"id": "pkg-news", "name": "News", "is_default": False, "channel_ids": [CH2]}
    )
    r101 = room(R101)
    r101["package_id"] = "pkg-news"
    hub.apply_room(copy.deepcopy(r101))
    await hass.async_block_till_done()
    assert hass.states.get(TV).attributes["source_list"] == ["2 · bTV"]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {"entity_id": TV, "source": "1 · BNT 1"},
            blocking=True,
        )
    assert calls(aioclient_mock, "post", f"{API}/rooms/{R101}/command") == []

    # Premium lists a disabled channel: it is left out.
    r101["package_id"] = PKG_PREMIUM
    hub.apply_room(copy.deepcopy(r101))
    await hass.async_block_till_done()
    assert hass.states.get(TV).attributes["source_list"] == ["1 · BNT 1", "2 · bTV"]

    # A package the integration does not know: every enabled channel, by number.
    r101["package_id"] = "pkg-unknown"
    hub.apply_room(copy.deepcopy(r101))
    await hass.async_block_till_done()
    assert hass.states.get(TV).attributes["source_list"] == ["1 · BNT 1", "2 · bTV"]
