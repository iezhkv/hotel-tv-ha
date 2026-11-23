"""Fixtures for the Hotel TV tests: a mocked /api/v1 of one property."""

from __future__ import annotations

from collections.abc import Callable
import copy
import json
import time
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
from custom_components.hotel_tv.signing import PATH_WEBHOOK, signature_header

# The real loop, kept before the autouse fixture patches it out.
REAL_RUN_EVENTS = HotelTvHub._async_run_events

SERVER = "http://hoteltv.local:3210"
API = SERVER + "/api/v1"
TOKEN = "htv_testtoken_0123456789"
SECRET = "5ec7e7" * 10 + "abcd"
CONNECTION = "77777777-7777-4777-8777-777777777777"
WEBHOOK_ID = "hotel_tv_test_webhook"
PROPERTY = "99999999-9999-4999-8999-999999999999"
TV101 = "22222222-2222-4222-8222-222222222101"
TV102 = "22222222-2222-4222-8222-222222222102"
CH1 = "33333333-3333-4333-8333-333333333001"
CH2 = "33333333-3333-4333-8333-333333333002"
CH3 = "33333333-3333-4333-8333-333333333003"
PKG_DEFAULT = "44444444-4444-4444-8444-444444444001"
PKG_PREMIUM = "44444444-4444-4444-8444-444444444002"

ENTRY_DATA = {
    "url": SERVER,
    "token": TOKEN,
    "secret": SECRET,
    "connection_id": CONNECTION,
    "webhook_id": WEBHOOK_ID,
    "property_id": PROPERTY,
    "property_name": "Hotel Demo",
}

INFO = {
    "property": {"id": PROPERTY, "name": "Hotel Demo"},
    "version": "2.0",
    "capabilities": ["devices", "device_commands", "controls", "events"],
}

TVS: list[dict[str, Any]] = [
    {
        "id": TV101,
        "name": "TV 101",
        "kind": "android",
        "paired": True,
        "online": True,
        "last_seen_at": "2026-11-18T09:00:00Z",
        "power": True,
        "channel_id": CH1,
        "channel_number": 1,
        "channel_name": "BNT 1",
        "volume": 40,
        "muted": False,
        "package_id": None,
        "controls": [],
    },
    {
        "id": TV102,
        "name": "TV 102",
        "kind": "linux",
        "paired": True,
        "online": False,
        "last_seen_at": "2026-11-17T09:00:00Z",
        "power": False,
        "channel_id": None,
        "channel_number": None,
        "channel_name": None,
        "volume": 20,
        "muted": False,
        "package_id": PKG_PREMIUM,
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


def tv(tv_id: str) -> dict[str, Any]:
    """A deep copy of a fixture TV."""
    return copy.deepcopy(next(t for t in TVS if t["id"] == tv_id))


def mock_server(
    aioclient_mock: AiohttpClientMocker,
    *,
    tvs: list[dict[str, Any]] | None = None,
) -> None:
    """Register the read endpoints and accept every write."""
    tvs = copy.deepcopy(tvs if tvs is not None else TVS)
    aioclient_mock.get(f"{API}/info", json=INFO)
    aioclient_mock.get(f"{API}/devices", json={"devices": tvs})
    for t in tvs:
        aioclient_mock.get(f"{API}/devices/{t['id']}", json=t)
    aioclient_mock.get(f"{API}/channels", json={"channels": copy.deepcopy(CHANNELS)})
    aioclient_mock.get(f"{API}/packages", json={"packages": copy.deepcopy(PACKAGES)})
    aioclient_mock.get(f"{API}/control-requests", json={"requests": []})
    for t in tvs:
        aioclient_mock.put(f"{API}/devices/{t['id']}/controls", json={"controls": []})


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
        tvs: list[dict[str, Any]] | None = None,
        mock: bool = True,
    ) -> MockConfigEntry:
        if mock:
            mock_server(aioclient_mock, tvs=tvs)
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


def tv_device(hass: HomeAssistant, tv_id: str) -> dr.DeviceEntry:
    """The HA device of a TV."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    device = find_device(dr.async_get(hass), entry.entry_id, tv_id)
    assert device is not None
    return device


_ids = iter(range(1, 10**9))


def signed(
    key: str,
    message: dict[str, Any] | bytes,
    *,
    message_id: str | None = None,
    t: int | None = None,
    device: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """A webhook body and its signed headers (docs/HA-PAIRING.md §3)."""
    body = message if isinstance(message, bytes) else json.dumps(message).encode()
    message_id = message_id or f"msg-{next(_ids)}"
    t = int(time.time()) if t is None else t
    headers = {
        "Content-Type": "application/json",
        "X-HotelTV-Id": message_id,
        "X-HotelTV-Signature": signature_header(
            key, t, message_id, "POST", PATH_WEBHOOK, body
        ),
    }
    if device is not None:
        headers["X-HotelTV-Device"] = device
    return body, headers
