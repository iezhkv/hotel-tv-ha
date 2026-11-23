"""TV binary sensors: online."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HotelTvConfigEntry
from .entity import HotelTvDeviceEntity, setup_tv_platform

PARALLEL_UPDATES = 0

ONLINE = BinarySensorEntityDescription(
    key="online",
    translation_key="online",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HotelTvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the TV binary sensors."""
    hub = entry.runtime_data
    setup_tv_platform(
        hass,
        hub,
        async_add_entities,
        lambda tv_id: [HotelTvOnline(hub, tv_id, ONLINE)],
    )


class HotelTvOnline(HotelTvDeviceEntity, BinarySensorEntity):
    """The TV is connected to the server."""

    @property
    def is_on(self) -> bool:
        """Online."""
        return bool(self.tv.get("online"))
