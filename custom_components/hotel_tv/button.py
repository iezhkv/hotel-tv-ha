"""Buttons: reload / identify a room's TV, refresh the property's channels."""

from __future__ import annotations

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HotelTvConfigEntry
from .entity import HotelTvHubEntity, HotelTvRoomEntity, setup_room_platform

PARALLEL_UPDATES = 0

RELOAD = ButtonEntityDescription(
    key="reload",
    translation_key="reload",
    device_class=ButtonDeviceClass.RESTART,
    entity_category=EntityCategory.CONFIG,
)
IDENTIFY = ButtonEntityDescription(
    key="identify",
    translation_key="identify",
    device_class=ButtonDeviceClass.IDENTIFY,
    entity_category=EntityCategory.DIAGNOSTIC,
)
REFRESH_CHANNELS = ButtonEntityDescription(
    key="refresh_channels",
    translation_key="refresh_channels",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HotelTvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons."""
    hub = entry.runtime_data
    async_add_entities([HotelTvRefreshChannels(hub, REFRESH_CHANNELS)])

    def build(room_id: str) -> list[Entity]:
        if not hub.tv(room_id):
            return []
        return [
            HotelTvCommandButton(hub, room_id, RELOAD),
            HotelTvCommandButton(hub, room_id, IDENTIFY),
        ]

    setup_room_platform(hass, hub, async_add_entities, build)


class HotelTvCommandButton(HotelTvRoomEntity, ButtonEntity):
    """Sends the command named by the description key to the room's TVs."""

    needs_tv = True

    async def async_press(self) -> None:
        """Send the command."""
        await self._async_command(self.entity_description.key)


class HotelTvRefreshChannels(HotelTvHubEntity, ButtonEntity):
    """Re-probe the channel sources and reload every TV."""

    async def async_press(self) -> None:
        """POST /channels/refresh."""
        await self._async_call(self.hub.client.async_refresh_channels())
