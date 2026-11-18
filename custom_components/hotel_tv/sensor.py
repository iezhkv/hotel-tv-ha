"""Room sensors: guest name, current channel."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HotelTvConfigEntry
from .entity import HotelTvRoomEntity, setup_room_platform

PARALLEL_UPDATES = 0

GUEST_NAME = SensorEntityDescription(key="guest_name", translation_key="guest_name")
CURRENT_CHANNEL = SensorEntityDescription(
    key="current_channel", translation_key="current_channel"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HotelTvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the room sensors."""
    hub = entry.runtime_data

    def build(room_id: str) -> list[Entity]:
        entities: list[Entity] = [HotelTvGuestName(hub, room_id, GUEST_NAME)]
        if hub.tv(room_id):
            entities.append(HotelTvCurrentChannel(hub, room_id, CURRENT_CHANNEL))
        return entities

    setup_room_platform(hass, hub, async_add_entities, build)


class HotelTvGuestName(HotelTvRoomEntity, SensorEntity):
    """The checked-in guest's name (unknown when the room is free)."""

    @property
    def native_value(self) -> str | None:
        """Guest name."""
        return self.room.get("guest_name") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Stay details."""
        room = self.room
        return {
            "guest_locale": room.get("guest_locale"),
            "welcome_message": room.get("welcome_message"),
            "checkin_at": room.get("checkin_at"),
            "checkout_at": room.get("checkout_at"),
            "room_name": room.get("name"),
            "floor": room.get("floor"),
            "room_id": self.room_id,
        }


class HotelTvCurrentChannel(HotelTvRoomEntity, SensorEntity):
    """What the TV shows: the channel name (unknown when off)."""

    needs_tv = True

    @property
    def native_value(self) -> str | None:
        """Channel name."""
        tv = self.tv or {}
        if not tv.get("power"):
            return None
        return tv.get("channel_name") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Channel number and id."""
        tv = self.tv or {}
        return {
            "channel_number": tv.get("channel_number"),
            "channel_id": tv.get("channel_id"),
        }
