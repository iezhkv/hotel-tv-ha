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
    PKG_DEFAULT,
    PKG_PREMIUM,
    REAL_RUN_EVENTS,
    SECRET,
    TOKEN,
    TV101,
    TV102,
    WEBHOOK_ID,
    calls,
    mock_server,
    tv,
    tv_device,
)

TV = "media_player.hotel_demo_tv_101"
TV_102 = "media_player.hotel_demo_tv_102"


async def _event(hass: HomeAssistant, entry, **event: Any) -> None:
    await entry.runtime_data.async_handle_event(event)
    await hass.async_block_till_done()


async def test_entities_created(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """Every TV is a device with its media player, sensors, buttons and select."""
    await setup_entry()
    registry = er.async_get(hass)
    for tv_id, slug in ((TV101, "hotel_demo_tv_101"), (TV102, "hotel_demo_tv_102")):
        ids = {
            e.entity_id
            for e in er.async_entries_for_device(registry, tv_device(hass, tv_id).id)
        }
        assert ids == {
            f"media_player.{slug}",
            f"binary_sensor.{slug}_online",
            f"sensor.{slug}_current_channel",
            f"button.{slug}_reload",
            f"button.{slug}_identify",
            f"select.{slug}_channel_package",
        }
    device = tv_device(hass, TV101)
    assert device.name == "Hotel Demo · TV 101"
    assert device.model == "Android TV"
    assert (
        device.configuration_url == f"http://hoteltv.local:3210/admin/devices/{TV101}"
    )
    assert tv_device(hass, TV102).model == "Linux TV"
    assert hass.states.get("button.hotel_demo_refresh_channels") is not None


async def test_states(hass: HomeAssistant, setup_entry: Callable[..., Any]) -> None:
    """Initial states from /devices, /channels, /packages."""
    await setup_entry()
    state = hass.states.get(TV)
    assert state.state == STATE_ON
    assert state.attributes["source"] == "1 · BNT 1"
    # The default package's channels, in package order.
    assert state.attributes["source_list"] == ["2 · bTV", "1 · BNT 1"]
    assert state.attributes["volume_level"] == 0.4
    assert state.attributes["is_volume_muted"] is False
    assert state.attributes["media_title"] == "BNT 1"
    assert state.attributes["channel_number"] == 1
    assert state.attributes["tv_id"] == TV101
    assert state.attributes["kind"] == "android"
    # An offline TV: its player is unavailable, the rest of the device is not.
    assert hass.states.get(TV_102).state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.hotel_demo_tv_101_online").state == STATE_ON
    assert hass.states.get("binary_sensor.hotel_demo_tv_102_online").state == STATE_OFF
    assert hass.states.get("sensor.hotel_demo_tv_101_current_channel").state == "BNT 1"
    assert (
        hass.states.get("sensor.hotel_demo_tv_102_current_channel").state
        == STATE_UNKNOWN
    )
    # No own package = the default one; TV 102 has its own.
    assert (
        hass.states.get("select.hotel_demo_tv_101_channel_package").state == "Standard"
    )
    assert (
        hass.states.get("select.hotel_demo_tv_102_channel_package").state == "Premium"
    )


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
        device_id=TV101,
        payload={
            "power": True,
            "channel_id": CH2,
            "channel_number": 2,
            "channel_name": "bTV",
            "volume": 15,
            "muted": True,
        },
    )
    state = hass.states.get(TV)
    assert state.attributes["source"] == "2 · bTV"
    assert state.attributes["volume_level"] == 0.15
    assert state.attributes["is_volume_muted"] is True
    assert hass.states.get("sensor.hotel_demo_tv_101_current_channel").state == "bTV"

    await _event(hass, entry, type="device.offline", device_id=TV101)
    assert hass.states.get(TV).state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.hotel_demo_tv_101_online").state == STATE_OFF
    await _event(hass, entry, type="device.online", device_id=TV101)
    assert hass.states.get(TV).state == STATE_ON

    await _event(
        hass, entry, type="device.state", device_id=TV101, payload={"power": False}
    )
    assert hass.states.get(TV).state == STATE_OFF
    assert (
        hass.states.get("sensor.hotel_demo_tv_101_current_channel").state
        == STATE_UNKNOWN
    )


async def test_device_updated_refetches(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """device.updated applies the payload, renames the device and re-reads the TV."""
    entry = await setup_entry()
    fresh = tv(TV102)
    fresh.update(name="TV 102 Suite", package_id=None)
    aioclient_mock.clear_requests()
    mock_server(aioclient_mock, tvs=[tv(TV101), fresh])
    await _event(
        hass,
        entry,
        type="device.updated",
        device_id=TV102,
        payload={"name": "TV 102 Suite", "package_id": None},
    )
    assert (
        hass.states.get("select.hotel_demo_tv_102_channel_package").state == "Standard"
    )
    assert tv_device(hass, TV102).name == "Hotel Demo · TV 102 Suite"
    assert calls(aioclient_mock, "get", f"{API}/devices/{TV102}") == [None]

    # A TV the server no longer has (404) loses its device.
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{API}/devices/{TV102}",
        status=404,
        json={"error": {"code": "not_found", "message": "Device not found"}},
    )
    await _event(hass, entry, type="device.updated", device_id=TV102, payload={})
    from homeassistant.helpers import device_registry as dr

    from custom_components.hotel_tv.hub import find_device

    assert find_device(dr.async_get(hass), entry.entry_id, TV102) is None


async def test_new_tv(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A TV that appears gets its entities on refresh (also via an unknown TV's event)."""
    entry = await setup_entry()
    tv103 = {**tv(TV101), "id": "tv-103", "name": "TV 103"}
    aioclient_mock.clear_requests()
    mock_server(aioclient_mock, tvs=[tv(TV101), tv(TV102), tv103])
    await _event(hass, entry, type="device.online", device_id="tv-103")
    await hass.async_block_till_done()
    assert hass.states.get("media_player.hotel_demo_tv_103").state == STATE_ON
    assert calls(aioclient_mock, "get", f"{API}/devices") == [None]


async def test_tv_removed_and_server_down(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A TV gone from the server loses its device; an unreachable server = unavailable."""
    entry = await setup_entry()
    aioclient_mock.clear_requests()
    mock_server(aioclient_mock, tvs=[tv(TV101)])
    assert await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    from homeassistant.helpers import device_registry as dr

    from custom_components.hotel_tv.hub import find_device

    assert find_device(dr.async_get(hass), entry.entry_id, TV102) is None

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/devices", exc=ClientError())
    aioclient_mock.get(f"{API}/channels", exc=ClientError())
    aioclient_mock.get(f"{API}/packages", exc=ClientError())
    assert not await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(TV).state == STATE_UNAVAILABLE
    assert (
        hass.states.get("select.hotel_demo_tv_101_channel_package").state
        == STATE_UNAVAILABLE
    )


async def test_old_room_devices_are_removed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Room devices left over from API 1.x are dropped on setup."""
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import ENTRY_DATA, PROPERTY

    mock_server(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Hotel Demo", unique_id=PROPERTY, data=dict(ENTRY_DATA)
    )
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    old = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "11111111-1111-4111-8111-111111111101")},
        name="Hotel Demo · 101",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.id) is None
    assert tv_device(hass, TV101) is not None


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
    select = hass.states.get("select.hotel_demo_tv_101_channel_package")
    assert select.attributes["options"] == ["Basic"]


async def test_bus_event(hass: HomeAssistant, setup_entry: Callable[..., Any]) -> None:
    """Every server event is fired as hotel_tv_event with the TV's HA device id."""
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
                    "device_id": TV101,
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
    assert data["tv_id"] == TV101
    assert data["tv_name"] == "TV 101"
    assert data["device_id"] == tv_device(hass, TV101).id
    assert data["payload"] == {"message_id": "m1", "title": "Hi"}
    assert "room_id" not in data
    assert entry.runtime_data.last_event_id == "77"


async def test_media_player_commands(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Each media player action is one command to the TV."""
    await setup_entry()
    url = f"{API}/devices/{TV101}/command"
    aioclient_mock.post(url, json={"ok": True})
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
    """Reload / identify / refresh channels; the package select PATCHes the TV."""
    await setup_entry()
    cmd = f"{API}/devices/{TV101}/command"
    aioclient_mock.post(cmd, json={"ok": True})
    aioclient_mock.post(
        f"{API}/channels/refresh", json={"ok": True, "channels": 2, "started": True}
    )
    patched = tv(TV101)
    patched["package_id"] = PKG_PREMIUM
    aioclient_mock.patch(f"{API}/devices/{TV101}", json=patched)
    for entity_id in (
        "button.hotel_demo_tv_101_reload",
        "button.hotel_demo_tv_101_identify",
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

    select = "select.hotel_demo_tv_101_channel_package"
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select, "option": "Premium"},
        blocking=True,
    )
    assert calls(aioclient_mock, "patch", f"{API}/devices/{TV101}") == [
        {"package_id": PKG_PREMIUM}
    ]
    await hass.async_block_till_done()
    assert hass.states.get(select).state == "Premium"
    # Back to the default package = follow the default (null).
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select, "option": "Standard"},
        blocking=True,
    )
    assert calls(aioclient_mock, "patch", f"{API}/devices/{TV101}")[-1] == {
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


async def test_entry_without_secret_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, setup_entry
) -> None:
    """An entry made with a typed token (before pairing) asks for a pairing code."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import ENTRY_DATA, PROPERTY

    mock_server(aioclient_mock)
    legacy = {
        k: v
        for k, v in ENTRY_DATA.items()
        if k not in ("secret", "connection_id", "webhook_id")
    }
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=legacy)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert not aioclient_mock.mock_calls


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
        f'"device_id": "{TV101}", "payload": {{"volume": 70}}}}\n\n'
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
    # Applied (then a full refresh after the stream ended re-read the TVs).
    assert [(e.data["type"], e.data["payload"]) for e in bus] == [
        ("device.state", {"volume": 70})
    ]
    assert hub.last_event_id == "5"
    assert len(calls(aioclient_mock, "get", f"{API}/devices")) >= 2
    assert hub.stream_connected is False
    assert "Last-Event-ID" not in seen_headers[0]
    assert seen_headers[1]["Last-Event-ID"] == "5"
    assert seen_headers[1]["Authorization"] == f"Bearer {TOKEN}"


async def test_diagnostics(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """The token, the secret and the webhook id are redacted; the TVs are listed."""
    entry = await setup_entry()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    for key in ("token", "secret", "webhook_id"):
        assert diag["entry"]["data"][key] == "**REDACTED**"
    dumped = json.dumps(diag)
    assert TOKEN not in dumped
    assert SECRET not in dumped
    assert WEBHOOK_ID not in dumped
    assert [t["id"] for t in diag["tvs"]] == [TV101, TV102]
    assert diag["channels"] == 3


async def test_source_list_follows_package(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The TV's own package limits the sources; unknown package = all enabled."""
    from homeassistant.exceptions import ServiceValidationError
    import pytest

    entry = await setup_entry()
    hub = entry.runtime_data
    aioclient_mock.post(f"{API}/devices/{TV101}/command", json={"ok": True})

    # Own package with one channel: only that one, and others are refused.
    hub.packages.append(
        {"id": "pkg-news", "name": "News", "is_default": False, "channel_ids": [CH2]}
    )
    tv101 = tv(TV101)
    tv101["package_id"] = "pkg-news"
    hub.apply_tv(copy.deepcopy(tv101))
    await hass.async_block_till_done()
    assert hass.states.get(TV).attributes["source_list"] == ["2 · bTV"]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {"entity_id": TV, "source": "1 · BNT 1"},
            blocking=True,
        )
    assert calls(aioclient_mock, "post", f"{API}/devices/{TV101}/command") == []

    # Premium lists a disabled channel: it is left out.
    tv101["package_id"] = PKG_PREMIUM
    hub.apply_tv(copy.deepcopy(tv101))
    await hass.async_block_till_done()
    assert hass.states.get(TV).attributes["source_list"] == ["1 · BNT 1", "2 · bTV"]

    # A package the integration does not know: every enabled channel, by number.
    tv101["package_id"] = "pkg-unknown"
    hub.apply_tv(copy.deepcopy(tv101))
    await hass.async_block_till_done()
    assert hass.states.get(TV).attributes["source_list"] == ["1 · BNT 1", "2 · bTV"]
