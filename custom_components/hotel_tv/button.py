"""Buttons: reload / identify a TV, refresh the property's channels."""

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
from .entity import HotelTvDeviceEntity, HotelTvHubEntity, setup_tv_platform

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

    def build(tv_id: str) -> list[Entity]:
        return [
            HotelTvCommandButton(hub, tv_id, RELOAD),
            HotelTvCommandButton(hub, tv_id, IDENTIFY),
        ]

    setup_tv_platform(hass, hub, async_add_entities, build)


class HotelTvCommandButton(HotelTvDeviceEntity, ButtonEntity):
    """Sends the command named by the description key to the TV."""

    async def async_press(self) -> None:
        """Send the command."""
        await self._async_command(self.entity_description.key)


class HotelTvRefreshChannels(HotelTvHubEntity, ButtonEntity):
    """Re-probe the channel sources and reload every TV."""

    async def async_press(self) -> None:
        """POST /channels/refresh."""
        await self._async_call(self.hub.client.async_refresh_channels())
