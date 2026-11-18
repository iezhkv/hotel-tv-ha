"""Diagnostics: connection, rooms and exposed controls, without the token or guest data."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

from . import HotelTvConfigEntry

TO_REDACT_ENTRY = {CONF_TOKEN}
TO_REDACT_ROOM = {"guest_name", "welcome_message"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HotelTvConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    hub = entry.runtime_data
    bridge = hub.bridge
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY),
            "options": dict(entry.options),
        },
        "server": {
            "version": hub.info.get("version"),
            "capabilities": hub.info.get("capabilities"),
        },
        "connection": {
            "available": hub.available,
            "stream_connected": hub.stream_connected,
            "last_event_id": hub.last_event_id,
            "last_event_at": hub.last_event_at,
            "last_refresh_at": hub.last_refresh_at,
        },
        "channels": len(hub.channels),
        "packages": hub.packages,
        "rooms": [
            async_redact_data(room, TO_REDACT_ROOM) for room in hub.rooms.values()
        ],
        "controls": {
            "exposed": bridge.mapping if bridge else {},
            "sent": bridge.sent if bridge else {},
            "last_error": bridge.last_error if bridge else None,
        },
    }
