"""The hotel_tv.* services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.hotel_tv.const import DOMAIN

from .conftest import API, PROPERTY, TV101, TV102, calls, tv_device


async def test_send_message(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """One message per targeted TV; device and entity targets resolve to TVs."""
    await setup_entry()
    aioclient_mock.post(f"{API}/messages", json={"id": "m-1"})
    response = await hass.services.async_call(
        DOMAIN,
        "send_message",
        {
            "device_id": [tv_device(hass, TV101).id],
            "entity_id": [
                "sensor.hotel_demo_tv_102_current_channel",
                "media_player.hotel_demo_tv_101",
            ],
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
    assert sorted(bodies, key=lambda b: b["device_id"]) == [
        {**common, "device_id": TV101},
        {**common, "device_id": TV102},
    ]
    assert sorted(r["tv_id"] for r in response["messages"]) == [TV101, TV102]


async def test_send_message_all_tvs(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """all_tvs without a target = one property-wide message (device_id null)."""
    await setup_entry()
    aioclient_mock.post(f"{API}/messages", json={"id": "m-all"})
    response = await hass.services.async_call(
        DOMAIN,
        "send_message",
        {"title": "Fire drill at 11:00", "all_tvs": True},
        blocking=True,
        return_response=True,
    )
    assert calls(aioclient_mock, "post", f"{API}/messages") == [
        {"title": "Fire drill at 11:00", "device_id": None}
    ]
    assert response == {
        "messages": [{"property_id": PROPERTY, "tv_id": None, "message_id": "m-all"}]
    }
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "send_message", {"title": "To nobody"}, blocking=True
        )


async def test_area_target(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An area targets the TVs in it; its other devices are skipped."""
    entry = await setup_entry()
    area = ar.async_get(hass).async_create("Room 101")
    dev_reg = dr.async_get(hass)
    dev_reg.async_update_device(tv_device(hass, TV101).id, area_id=area.id)
    dev_reg.async_update_device(entry.runtime_data.hub_device_id, area_id=area.id)
    aioclient_mock.post(f"{API}/devices/{TV101}/command", json={"ok": True})
    response = await hass.services.async_call(
        DOMAIN,
        "send_command",
        {"area_id": area.id, "command": "reload"},
        blocking=True,
        return_response=True,
    )
    assert response == {"tvs": [{"tv_id": TV101, "tv_name": "TV 101"}]}


async def test_send_command_and_errors(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Raw commands; the server's 422 becomes a HomeAssistantError."""
    await setup_entry()
    aioclient_mock.post(f"{API}/devices/{TV101}/command", json={"ok": True})
    aioclient_mock.post(
        f"{API}/devices/{TV102}/command",
        status=422,
        json={"error": {"code": "validation_failed", "message": "payload.volume"}},
    )
    response = await hass.services.async_call(
        DOMAIN,
        "send_command",
        {
            "entity_id": "media_player.hotel_demo_tv_101",
            "command": "open_panel",
            "payload": {"panel": "room"},
        },
        blocking=True,
        return_response=True,
    )
    assert response == {"tvs": [{"tv_id": TV101, "tv_name": "TV 101"}]}
    assert calls(aioclient_mock, "post", f"{API}/devices/{TV101}/command") == [
        {"type": "open_panel", "payload": {"panel": "room"}}
    ]
    with pytest.raises(HomeAssistantError, match=r"payload\.volume"):
        await hass.services.async_call(
            DOMAIN,
            "send_command",
            {"device_id": tv_device(hass, TV102).id, "command": "set_volume"},
            blocking=True,
        )
    with pytest.raises(Exception, match="command"):
        await hass.services.async_call(
            DOMAIN,
            "send_command",
            {"device_id": tv_device(hass, TV101).id, "command": "self_destruct"},
            blocking=True,
        )


async def test_not_a_tv(hass: HomeAssistant, setup_entry: Callable[..., Any]) -> None:
    """The property device or a foreign entity is not a TV."""
    entry = await setup_entry()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "send_command",
            {"device_id": entry.runtime_data.hub_device_id, "command": "reload"},
            blocking=True,
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "send_command",
            {"entity_id": "light.nowhere", "command": "reload"},
            blocking=True,
        )


async def test_guest_services_are_gone(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """No check-in / check-out / guest services any more."""
    await setup_entry()
    services = hass.services.async_services_for_domain(DOMAIN)
    assert set(services) == {"send_message", "send_command", "refresh_channels"}


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
