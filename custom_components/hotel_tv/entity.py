"""Base entities: one HA device per room, plus the property (hub) device."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import HotelTvAuthError, HotelTvError
from .const import DOMAIN, MANUFACTURER
from .hub import HotelTvHub, signal_new_entities, signal_room, signal_update


class HotelTvEntity(Entity):
    """Common to every Hotel TV entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: HotelTvHub, description: EntityDescription) -> None:
        """Initialize."""
        self.hub = hub
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Unavailable while the server is unreachable."""
        return self.hub.available

    async def async_added_to_hass(self) -> None:
        """Follow hub-wide updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self.hub.entry.entry_id), self._async_updated
            )
        )

    @callback
    def _async_updated(self) -> None:
        self.async_write_ha_state()

    async def _async_call(self, coro: Any) -> Any:
        """Run an API call, turning its errors into HomeAssistantError."""
        try:
            return await coro
        except HotelTvAuthError as err:
            self.hub.async_start_reauth()
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except HotelTvError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err


class HotelTvHubEntity(HotelTvEntity):
    """An entity of the property itself."""

    def __init__(self, hub: HotelTvHub, description: EntityDescription) -> None:
        """Initialize."""
        super().__init__(hub, description)
        self._attr_unique_id = f"{hub.property_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, hub.property_id)})


class HotelTvRoomEntity(HotelTvEntity):
    """An entity of one room device."""

    # The entity needs a TV in the room to be available.
    needs_tv = False

    def __init__(
        self, hub: HotelTvHub, room_id: str, description: EntityDescription
    ) -> None:
        """Initialize."""
        super().__init__(hub, description)
        self.room_id = room_id
        self._attr_unique_id = f"{room_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, room_id)},
            name=hub.device_name(room_id),
            manufacturer=MANUFACTURER,
            model="Room",
            configuration_url=f"{hub.client.url}/admin/rooms",
        )
        # Linked to the property device; `via_device_id` replaces `via_device`
        # from HA 2026.x (the old key is removed in 2027.8).
        if "via_device_id" in DeviceInfo.__annotations__:
            if hub.hub_device_id:
                self._attr_device_info["via_device_id"] = hub.hub_device_id  # type: ignore[typeddict-unknown-key]
        else:
            self._attr_device_info["via_device"] = (DOMAIN, hub.property_id)

    @property
    def room(self) -> dict[str, Any]:
        """The latest Room."""
        return self.hub.rooms.get(self.room_id) or {}

    @property
    def tv(self) -> dict[str, Any] | None:
        """The room's first TV device."""
        return self.hub.tv(self.room_id)

    @property
    def available(self) -> bool:
        """The server is reachable and still lists the room (and its TV)."""
        if not self.hub.available or self.room_id not in self.hub.rooms:
            return False
        return not self.needs_tv or self.tv is not None

    async def async_added_to_hass(self) -> None:
        """Follow the room's updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_room(self.hub.entry.entry_id, self.room_id),
                self._async_updated,
            )
        )

    async def _async_command(
        self, command: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Send a command to the room's TVs."""
        await self._async_call(
            self.hub.client.async_command(self.room_id, command, payload)
        )


def setup_room_platform(
    hass: HomeAssistant,
    hub: HotelTvHub,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[str], list[Entity]],
) -> None:
    """Add entities for every room now, and for rooms (or TVs) that appear later.

    `build(room_id)` returns the entities the room should have now; the ones already
    added are skipped.
    """
    added: set[str] = set()

    @callback
    def _add() -> None:
        new: list[Entity] = []
        for room_id in list(hub.rooms):
            for entity in build(room_id):
                if entity.unique_id and entity.unique_id not in added:
                    added.add(entity.unique_id)
                    new.append(entity)
        if new:
            async_add_entities(new)

    _add()
    hub.entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_entities(hub.entry.entry_id), _add)
    )
