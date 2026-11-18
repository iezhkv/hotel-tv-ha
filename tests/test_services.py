"""The hotel_tv.* services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.hotel_tv.const import DOMAIN

from .conftest import API, PROPERTY, R101, R102, calls, room, room_device


async def test_send_message(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """One message per targeted room; device and entity targets resolve to rooms."""
    await setup_entry()
    aioclient_mock.post(f"{API}/messages", json={"id": "m-1"})
    response = await hass.services.async_call(
        DOMAIN,
        "send_message",
        {
            "device_id": [room_device(hass, R101).id],
            "entity_id": ["sensor.hotel_demo_102_guest", "sensor.hotel_demo_101_guest"],
            "title": "Breakfast",
            "message": "Until 10:30",
            "kind": "promo",
            "display": "modal",
            "expires_in": {"hours": 1},
        },
        blocking=True,
        return_response=True,
    )
    bodies = calls(aioclient_mock, "post", f"{API}/messages")
    common = {
        "title": "Breakfast",
        "body": "Until 10:30",
        "kind": "promo",
        "display": "modal",
        "expires_in": 3600,
    }
    assert sorted(bodies, key=lambda b: b["room_id"]) == [
        {**common, "room_id": R101},
        {**common, "room_id": R102},
    ]
    assert len(response["messages"]) == 2


async def test_send_message_all_rooms(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """all_rooms without a target = one property-wide message (room_id null)."""
    await setup_entry()
    aioclient_mock.post(f"{API}/messages", json={"id": "m-all"})
    response = await hass.services.async_call(
        DOMAIN,
        "send_message",
        {"title": "Fire drill at 11:00", "all_rooms": True},
        blocking=True,
        return_response=True,
    )
    assert calls(aioclient_mock, "post", f"{API}/messages") == [
        {"title": "Fire drill at 11:00", "room_id": None}
    ]
    assert response == {
        "messages": [{"property_id": PROPERTY, "room_id": None, "message_id": "m-all"}]
    }
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "send_message", {"title": "To nobody"}, blocking=True
        )


async def test_check_in_out_and_guest(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """check_in / check_out / set_guest call the room endpoints and apply the answer."""
    await setup_entry()
    checked_in = room(R102)
    checked_in.update(occupied=True, guest_name="John Smith", guest_locale="en")
    aioclient_mock.post(f"{API}/rooms/{R102}/checkin", json=checked_in)
    target = {"device_id": room_device(hass, R102).id}
    await hass.services.async_call(
        DOMAIN,
        "check_in",
        {
            **target,
            "guest_name": "John Smith",
            "guest_locale": "en",
            "checkout_at": "2026-11-20T10:00:00+00:00",
        },
        blocking=True,
    )
    assert calls(aioclient_mock, "post", f"{API}/rooms/{R102}/checkin") == [
        {
            "guest_name": "John Smith",
            "guest_locale": "en",
            "checkout_at": "2026-11-20T10:00:00+00:00",
        }
    ]
    await hass.async_block_till_done()
    assert hass.states.get("sensor.hotel_demo_102_guest").state == "John Smith"
    assert hass.states.get("binary_sensor.hotel_demo_102_occupied").state == "on"

    patched = {**checked_in, "welcome_message": "Happy birthday!"}
    aioclient_mock.patch(f"{API}/rooms/{R102}", json=patched)
    await hass.services.async_call(
        DOMAIN,
        "set_guest",
        {**target, "welcome_message": "Happy birthday!", "guest_locale": ""},
        blocking=True,
    )
    assert calls(aioclient_mock, "patch", f"{API}/rooms/{R102}") == [
        {"welcome_message": "Happy birthday!", "guest_locale": None}
    ]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "set_guest", target, blocking=True)

    aioclient_mock.post(f"{API}/rooms/{R102}/checkout", json=room(R102))
    await hass.services.async_call(DOMAIN, "check_out", target, blocking=True)
    assert calls(aioclient_mock, "post", f"{API}/rooms/{R102}/checkout") == [{}]
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.hotel_demo_102_occupied").state == "off"


async def test_send_command_and_errors(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Raw commands; the server's 422 becomes a HomeAssistantError."""
    await setup_entry()
    aioclient_mock.post(f"{API}/rooms/{R101}/command", json={"ok": True, "devices": 1})
    aioclient_mock.post(
        f"{API}/rooms/{R102}/command",
        status=422,
        json={"error": {"code": "no_devices", "message": "Room has no devices"}},
    )
    response = await hass.services.async_call(
        DOMAIN,
        "send_command",
        {
            "entity_id": "media_player.hotel_demo_101_tv",
            "command": "open_panel",
            "payload": {"panel": "room"},
        },
        blocking=True,
        return_response=True,
    )
    assert response == {"rooms": [{"room_id": R101, "devices": 1}]}
    assert calls(aioclient_mock, "post", f"{API}/rooms/{R101}/command") == [
        {"type": "open_panel", "payload": {"panel": "room"}}
    ]
    with pytest.raises(HomeAssistantError, match="Room has no devices"):
        await hass.services.async_call(
            DOMAIN,
            "send_command",
            {"device_id": room_device(hass, R102).id, "command": "reload"},
            blocking=True,
        )
    with pytest.raises(Exception, match="command"):
        await hass.services.async_call(
            DOMAIN,
            "send_command",
            {"device_id": room_device(hass, R101).id, "command": "self_destruct"},
            blocking=True,
        )


async def test_not_a_room(hass: HomeAssistant, setup_entry: Callable[..., Any]) -> None:
    """The property device or a foreign entity is not a room."""
    entry = await setup_entry()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "check_out",
            {"device_id": entry.runtime_data.hub_device_id},
            blocking=True,
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "check_out", {"entity_id": "light.nowhere"}, blocking=True
        )


async def test_refresh_channels(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Fire-and-forget by default; wait=true waits for the probe result."""
    await setup_entry()
    aioclient_mock.post(
        f"{API}/channels/refresh", json={"ok": True, "channels": 3, "started": True}
    )
    response = await hass.services.async_call(
        DOMAIN, "refresh_channels", {}, blocking=True, return_response=True
    )
    assert response == {
        "properties": [
            {"property_id": PROPERTY, "ok": True, "channels": 3, "started": True}
        ]
    }
    await hass.services.async_call(
        DOMAIN, "refresh_channels", {"wait": True}, blocking=True
    )
    urls = [str(u) for m, u, _d, _h in aioclient_mock.mock_calls if "refresh" in str(u)]
    assert urls == [f"{API}/channels/refresh?wait=false", f"{API}/channels/refresh"]
