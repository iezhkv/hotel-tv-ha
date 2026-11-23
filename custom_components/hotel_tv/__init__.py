"""The Hotel TV integration: the TVs of a hotel in Home Assistant."""

from __future__ import annotations

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import HotelTvAuthError, HotelTvClient, HotelTvError
from .bridge import ControlsBridge
from .const import CONF_PROPERTY_ID, CONF_SECRET, CONF_WEBHOOK_ID, DOMAIN
from .hub import HotelTvHub
from .services import async_setup_services
from .wire import WireHandler

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HotelTvConfigEntry = ConfigEntry[HotelTvHub]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (once, for every property)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HotelTvConfigEntry) -> bool:
    """Set up one property."""
    if not entry.data.get(CONF_SECRET) or not entry.data.get(CONF_WEBHOOK_ID):
        # Made with a typed API token (before pairing existed): pair with a code.
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="pairing_required"
        )
    client = HotelTvClient(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN]
    )
    try:
        info = await client.async_get_info()
        if str(info["property"]["id"]) != entry.data[CONF_PROPERTY_ID]:
            # The token now belongs to another property.
            raise HotelTvAuthError("wrong_property")
        hub = HotelTvHub(hass, entry, client, info)
        await hub.async_load()
    except HotelTvAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except HotelTvError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    hub.bridge = ControlsBridge(hass, entry, hub)
    hub.wire = WireHandler(hass, entry, hub)
    entry.runtime_data = hub
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    webhook.async_register(
        hass,
        DOMAIN,
        f"Hotel TV · {hub.property_name}",
        webhook_id,
        hub.wire.async_handle_webhook,
        local_only=False,
        allowed_methods=["POST"],
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hub.async_start()
    hub.bridge.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: HotelTvConfigEntry
) -> None:
    """Options changed: the controls exposed to the TVs."""
    hub = entry.runtime_data
    if hub.bridge is not None:
        await hub.bridge.async_options_updated(entry.options)


async def async_unload_entry(hass: HomeAssistant, entry: HotelTvConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: HotelTvConfigEntry, device: dr.DeviceEntry
) -> bool:
    """A TV device may be deleted by hand only when the server no longer lists it."""
    hub = entry.runtime_data
    return not any(
        ident[0] == DOMAIN and (ident[1] in hub.tvs or ident[1] == hub.property_id)
        for ident in device.identifiers
    )
