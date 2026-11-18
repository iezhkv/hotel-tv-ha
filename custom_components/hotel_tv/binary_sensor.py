"""Room binary sensors: TV online, room occupied."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HotelTvConfigEntry
from .entity import HotelTvRoomEntity, setup_room_platform

PARALLEL_UPDATES = 0

TV_ONLINE = BinarySensorEntityDescription(
    key="tv_online",
    translation_key="tv_online",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)
OCCUPIED = BinarySensorEntityDescription(
    key="occupied",
    translation_key="occupied",
    device_class=BinarySensorDeviceClass.OCCUPANCY,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HotelTvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the room binary sensors."""
    hub = entry.runtime_data

    def build(room_id: str) -> list[Entity]:
        entities: list[Entity] = [HotelTvOccupied(hub, room_id, OCCUPIED)]
        if hub.tv(room_id):
            entities.append(HotelTvOnline(hub, room_id, TV_ONLINE))
        return entities

    setup_room_platform(hass, hub, async_add_entities, build)


class HotelTvOccupied(HotelTvRoomEntity, BinarySensorEntity):
    """A guest is checked in (the room's `occupied` flag)."""

    @property
    def is_on(self) -> bool:
        """Occupied."""
        return bool(self.room.get("occupied"))


class HotelTvOnline(HotelTvRoomEntity, BinarySensorEntity):
    """The room's TV is connected to the server."""

    needs_tv = True

    @property
    def is_on(self) -> bool:
        """Online."""
        tv = self.tv
        return bool(tv and tv.get("online"))
