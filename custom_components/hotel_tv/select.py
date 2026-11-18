"""The room's channel package."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HotelTvConfigEntry
from .const import DOMAIN
from .entity import HotelTvRoomEntity, setup_room_platform

PARALLEL_UPDATES = 0

PACKAGE = SelectEntityDescription(
    key="package", translation_key="package", entity_category=EntityCategory.CONFIG
)


def package_options(packages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{option label: package}; duplicate names get a suffix to stay unique."""
    options: dict[str, dict[str, Any]] = {}
    for package in packages:
        label = str(package.get("name") or package.get("id"))
        candidate, n = label, 2
        while candidate in options:
            candidate = f"{label} ({n})"
            n += 1
        options[candidate] = package
    return options


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HotelTvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the package select of every room."""
    hub = entry.runtime_data
    setup_room_platform(
        hass,
        hub,
        async_add_entities,
        lambda room_id: [HotelTvPackageSelect(hub, room_id, PACKAGE)],
    )


class HotelTvPackageSelect(HotelTvRoomEntity, SelectEntity):
    """Which channel package the room's TVs show.

    A room without its own package follows the property's default package; choosing
    the default package sets the room back to following it.
    """

    @property
    def options(self) -> list[str]:
        """Package names."""
        return list(package_options(self.hub.packages))

    @property
    def current_option(self) -> str | None:
        """The room's package, or the default one."""
        package_id = self.room.get("package_id")
        for label, package in package_options(self.hub.packages).items():
            if (package_id and package.get("id") == package_id) or (
                not package_id and package.get("is_default")
            ):
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """PATCH the room's package_id."""
        package = package_options(self.hub.packages).get(option)
        if package is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_package",
                translation_placeholders={"package": option},
            )
        package_id = None if package.get("is_default") else package.get("id")
        room = await self._async_call(
            self.hub.client.async_update_room(self.room_id, {"package_id": package_id})
        )
        self.hub.apply_room(room)
