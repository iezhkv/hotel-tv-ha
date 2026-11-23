"""TV sensors: the current channel."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HotelTvConfigEntry
from .entity import HotelTvDeviceEntity, setup_tv_platform

PARALLEL_UPDATES = 0

CURRENT_CHANNEL = SensorEntityDescription(
    key="current_channel", translation_key="current_channel"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HotelTvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the TV sensors."""
    hub = entry.runtime_data
    setup_tv_platform(
        hass,
        hub,
        async_add_entities,
        lambda tv_id: [HotelTvCurrentChannel(hub, tv_id, CURRENT_CHANNEL)],
    )


class HotelTvCurrentChannel(HotelTvDeviceEntity, SensorEntity):
    """What the TV shows: the channel name (unknown when off)."""

    @property
    def native_value(self) -> str | None:
        """Channel name."""
        tv = self.tv
        if not tv.get("power"):
            return None
        return tv.get("channel_name") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Channel number and id."""
        tv = self.tv
        return {
            "channel_number": tv.get("channel_number"),
            "channel_id": tv.get("channel_id"),
        }
