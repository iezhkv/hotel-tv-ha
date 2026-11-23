"""Opt-in: the whole integration against a running Hotel TV server.

    HOTEL_TV_LIVE_URL=http://127.0.0.1:3210 HOTEL_TV_LIVE_TOKEN=htv_… pytest tests/test_live.py

Read-only apart from an empty `PATCH /devices/{id}` (emits device.updated, changes
nothing). Use an IP address (the test harness cannot resolve host names).
No controls are exposed, so the server's controls are not touched.
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
    """Entities for every TV from the real server; a real event arrives by SSE."""
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
            # Not paired: the webhook is registered but nothing posts to it.
            "secret": "live-test-unpaired",
            "webhook_id": "hotel_tv_live_test",
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
    assert len(players) == len(hub.tvs)
    assert len([e for e in entities if e.translation_key == "online"]) == len(hub.tvs)
    for entity_id in players:
        state = hass.states.get(entity_id)
        assert state is not None
        assert (
            len(state.attributes.get("source_list") or []) > 0
            or state.state == "unavailable"
        )

    # Sources follow the TV's package (channel_ids, in package order).
    for tv_id, tv in hub.tvs.items():
        entity_id = registry.async_get_entity_id("media_player", DOMAIN, f"{tv_id}_tv")
        expected = [c["id"] for c in hub.tv_channels(tv_id)]
        package = next(
            p
            for p in hub.packages
            if (p["id"] == tv["package_id"] if tv["package_id"] else p["is_default"])
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
    tv_id = next(iter(hub.tvs))
    await hub.client.async_update_tv(tv_id, {})
    for _ in range(100):
        if any(e.data["type"] == "device.updated" for e in events):
            break
        await asyncio.sleep(0.1)
    event = next(e for e in events if e.data["type"] == "device.updated")
    assert event.data["tv_id"] == tv_id
    assert event.data["device_id"] is not None
    assert hub.last_event_id

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
