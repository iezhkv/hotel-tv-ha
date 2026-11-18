"""Fixtures for the Hotel TV tests: a mocked /api/v1 of one property."""

from __future__ import annotations

from collections.abc import Callable
import copy
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.hotel_tv.const import DOMAIN
from custom_components.hotel_tv.hub import HotelTvHub, find_device

# The real loop, kept before the autouse fixture patches it out.
REAL_RUN_EVENTS = HotelTvHub._async_run_events

SERVER = "http://hoteltv.local:3210"
API = SERVER + "/api/v1"
TOKEN = "htv_testtoken_0123456789"
PROPERTY = "99999999-9999-4999-8999-999999999999"
R101 = "11111111-1111-4111-8111-111111111101"
R102 = "11111111-1111-4111-8111-111111111102"
DEV101 = "22222222-2222-4222-8222-222222222101"
CH1 = "33333333-3333-4333-8333-333333333001"
CH2 = "33333333-3333-4333-8333-333333333002"
CH3 = "33333333-3333-4333-8333-333333333003"
PKG_DEFAULT = "44444444-4444-4444-8444-444444444001"
PKG_PREMIUM = "44444444-4444-4444-8444-444444444002"

ENTRY_DATA = {
    "url": SERVER,
    "token": TOKEN,
    "property_id": PROPERTY,
    "property_name": "Hotel Demo",
}

INFO = {
    "property": {"id": PROPERTY, "name": "Hotel Demo"},
    "version": "1.0.0",
    "capabilities": ["rooms", "controls", "events"],
}

ROOMS: list[dict[str, Any]] = [
    {
        "id": R101,
        "name": "101",
        "floor": "1",
        "occupied": True,
        "guest_name": "Maria Ivanova",
        "guest_locale": "bg",
        "welcome_message": None,
        "package_id": None,
        "checkin_at": "2026-11-17T12:00:00Z",
        "checkout_at": "2026-11-20T10:00:00Z",
        "devices": [
            {
                "id": DEV101,
                "name": "TV 101",
                "kind": "android",
                "online": True,
                "last_seen_at": "2026-11-18T09:00:00Z",
                "power": True,
                "channel_id": CH1,
                "channel_number": 1,
                "channel_name": "BNT 1",
                "volume": 40,
                "muted": False,
            }
        ],
        "controls": [],
    },
    {
        "id": R102,
        "name": "102",
        "floor": "1",
        "occupied": False,
        "guest_name": None,
        "guest_locale": None,
        "welcome_message": None,
        "package_id": PKG_PREMIUM,
        "checkin_at": None,
        "checkout_at": None,
        "devices": [],
        "controls": [],
    },
]

CHANNELS = [
    {
        "id": CH1,
        "number": 1,
        "name": "BNT 1",
        "category": "news",
        "kind": "tv",
        "enabled": True,
        "status": "ok",
    },
    {
        "id": CH2,
        "number": 2,
        "name": "bTV",
        "category": "general",
        "kind": "tv",
        "enabled": True,
        "status": "ok",
    },
    {
        "id": CH3,
        "number": 3,
        "name": "Off air",
        "category": "general",
        "kind": "tv",
        "enabled": False,
        "status": "error",
    },
]

PACKAGES = [
    # Package order differs from channel numbers on purpose.
    {
        "id": PKG_DEFAULT,
        "name": "Standard",
        "is_default": True,
        "channel_count": 2,
        "channel_ids": [CH2, CH1],
    },
    {
        "id": PKG_PREMIUM,
        "name": "Premium",
        "is_default": False,
        "channel_count": 3,
        "channel_ids": [CH1, CH2, CH3],
    },
]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/hotel_tv."""


@pytest.fixture(autouse=True)
def no_event_stream():
    """The SSE loop is tested on its own; entry setups do not open it."""
    with patch(
        "custom_components.hotel_tv.hub.HotelTvHub._async_run_events",
        new_callable=lambda: AsyncMock(return_value=None),
    ) as mock:
        yield mock


def room(room_id: str) -> dict[str, Any]:
    """A deep copy of a fixture room."""
    return copy.deepcopy(next(r for r in ROOMS if r["id"] == room_id))


def mock_server(
    aioclient_mock: AiohttpClientMocker,
    *,
    rooms: list[dict[str, Any]] | None = None,
) -> None:
    """Register the read endpoints and accept every write."""
    rooms = copy.deepcopy(rooms if rooms is not None else ROOMS)
    aioclient_mock.get(f"{API}/info", json=INFO)
    aioclient_mock.get(f"{API}/rooms", json={"rooms": rooms})
    for r in rooms:
        aioclient_mock.get(f"{API}/rooms/{r['id']}", json=r)
    aioclient_mock.get(f"{API}/channels", json={"channels": copy.deepcopy(CHANNELS)})
    aioclient_mock.get(f"{API}/packages", json={"packages": copy.deepcopy(PACKAGES)})
    aioclient_mock.get(f"{API}/control-requests", json={"requests": []})
    for r in rooms:
        aioclient_mock.put(f"{API}/rooms/{r['id']}/controls", json={"controls": []})


def calls(
    aioclient_mock: AiohttpClientMocker, method: str, url: str
) -> list[dict[str, Any] | None]:
    """The JSON bodies sent with METHOD to URL (or to URL prefix ending in '*')."""
    out: list[dict[str, Any] | None] = []
    for m, raw_url, data, _headers in aioclient_mock.mock_calls:
        if m.lower() != method.lower():
            continue
        u = str(raw_url)
        if (url.endswith("*") and u.startswith(url[:-1])) or u == url:
            out.append(json.loads(data) if data else None)
    return out


@pytest.fixture
def setup_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> Callable[..., Any]:
    """Set up an entry against the mocked server."""

    async def _setup(
        options: dict[str, Any] | None = None,
        rooms: list[dict[str, Any]] | None = None,
        mock: bool = True,
    ) -> MockConfigEntry:
        if mock:
            mock_server(aioclient_mock, rooms=rooms)
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Hotel Demo",
            unique_id=PROPERTY,
            data=dict(ENTRY_DATA),
            options=options or {},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup


def room_device(hass: HomeAssistant, room_id: str) -> dr.DeviceEntry:
    """The HA device of a room."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    device = find_device(dr.async_get(hass), entry.entry_id, room_id)
    assert device is not None
    return device
