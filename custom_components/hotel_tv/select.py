"""The TV's channel package."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HotelTvConfigEntry
from .const import DOMAIN
from .entity import HotelTvDeviceEntity, setup_tv_platform

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
    """Set up the package select of every TV."""
    hub = entry.runtime_data
    setup_tv_platform(
        hass,
        hub,
        async_add_entities,
        lambda tv_id: [HotelTvPackageSelect(hub, tv_id, PACKAGE)],
    )


class HotelTvPackageSelect(HotelTvDeviceEntity, SelectEntity):
    """Which channel package the TV shows.

    A TV without its own package follows the property's default package; choosing
    the default package sets the TV back to following it.
    """

    @property
    def options(self) -> list[str]:
        """Package names."""
        return list(package_options(self.hub.packages))

    @property
    def current_option(self) -> str | None:
        """The TV's package, or the default one."""
        package_id = self.tv.get("package_id")
        for label, package in package_options(self.hub.packages).items():
            if (package_id and package.get("id") == package_id) or (
                not package_id and package.get("is_default")
            ):
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """PATCH the TV's package_id."""
        package = package_options(self.hub.packages).get(option)
        if package is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_package",
                translation_placeholders={"package": option},
            )
        package_id = None if package.get("is_default") else package.get("id")
        tv = await self._async_call(
            self.hub.client.async_update_tv(self.tv_id, {"package_id": package_id})
        )
        self.hub.apply_tv(tv)
