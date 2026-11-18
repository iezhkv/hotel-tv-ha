"""Opt-in: the whole integration against a running Hotel TV server.

    HOTEL_TV_LIVE_URL=http://127.0.0.1:3210 HOTEL_TV_LIVE_TOKEN=htv_… pytest tests/test_live.py

Read-only apart from an empty `PATCH /rooms/{id}` (emits room.updated, changes nothing).
Use an IP address (the test harness cannot resolve host names).
No room controls are exposed, so the server's controls are not touched.
"""

from __future__ import annotations

import asyncio
import os

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.hotel_tv.const import DOMAIN, EVENT_HOTEL_TV

from .conftest import REAL_RUN_EVENTS

LIVE_URL = os.environ.get("HOTEL_TV_LIVE_URL")
LIVE_TOKEN = os.environ.get("HOTEL_TV_LIVE_TOKEN")

pytestmark = pytest.mark.skipif(
    not (LIVE_URL and LIVE_TOKEN), reason="HOTEL_TV_LIVE_URL / _TOKEN not set"
)


@pytest.fixture
def no_event_stream():
    """Overrides the autouse stub: the real event loop runs here."""
    return None


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
async def test_live_setup_and_stream(hass: HomeAssistant, socket_enabled) -> None:
    """Entities for every room from the real server; a real event arrives by SSE."""
    from custom_components.hotel_tv.hub import HotelTvHub

    HotelTvHub._async_run_events = REAL_RUN_EVENTS  # undo a stub, if any
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.hotel_tv.api import HotelTvClient

    info = await HotelTvClient(
        async_get_clientsession(hass), LIVE_URL, LIVE_TOKEN
    ).async_get_info()
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=info["property"]["name"],
        unique_id=info["property"]["id"],
        data={
            "url": LIVE_URL,
            "token": LIVE_TOKEN,
            "property_id": info["property"]["id"],
            "property_name": info["property"]["name"],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = entry.runtime_data
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    players = [e.entity_id for e in entities if e.domain == "media_player"]
    rooms_with_tv = [r for r in hub.rooms.values() if r.get("devices")]
    assert len(players) == len(rooms_with_tv)
    assert len([e for e in entities if e.translation_key == "occupied"]) == len(
        hub.rooms
    )
    for entity_id in players:
        state = hass.states.get(entity_id)
        assert state is not None
        assert (
            len(state.attributes.get("source_list") or []) > 0
            or state.state == "unavailable"
        )

    # Sources follow the room's package (channel_ids, in package order).
    for room_id, room in hub.rooms.items():
        if not room.get("devices"):
            continue
        entity_id = registry.async_get_entity_id(
            "media_player", DOMAIN, f"{room_id}_tv"
        )
        expected = [c["id"] for c in hub.room_channels(room_id)]
        package = next(
            p
            for p in hub.packages
            if (
                p["id"] == room["package_id"] if room["package_id"] else p["is_default"]
            )
        )
        enabled = {c["id"] for c in hub.channels if c.get("enabled")}
        assert expected == [i for i in package["channel_ids"] if i in enabled]
        assert (
            len(hass.states.get(entity_id).attributes.get("source_list") or [])
            == len(expected)
            or hass.states.get(entity_id).state == "unavailable"
        )

    events = async_capture_events(hass, EVENT_HOTEL_TV)
    for _ in range(100):
        if hub.stream_connected:
            break
        await asyncio.sleep(0.1)
    assert hub.stream_connected
    room_id = next(iter(hub.rooms))
    await hub.client.async_update_room(room_id, {})
    for _ in range(100):
        if any(e.data["type"] == "room.updated" for e in events):
            break
        await asyncio.sleep(0.1)
    event = next(e for e in events if e.data["type"] == "room.updated")
    assert event.data["room_id"] == room_id
    assert event.data["device_id"] is not None
    assert hub.last_event_id

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
