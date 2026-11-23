"""Services: messages, raw commands, channels.

TVs are targeted by their Home Assistant device (or any entity / area of it).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
import voluptuous as vol

from .api import HotelTvAuthError, HotelTvError
from .const import (
    COMMAND_TYPES,
    DOMAIN,
    MESSAGE_DISPLAYS,
    MESSAGE_KINDS,
    SERVICE_REFRESH_CHANNELS,
    SERVICE_SEND_COMMAND,
    SERVICE_SEND_MESSAGE,
)

if TYPE_CHECKING:
    from .hub import HotelTvHub

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ALL_TVS = "all_tvs"

TARGET_FIELDS = cv.ENTITY_SERVICE_FIELDS


def _seconds(value: timedelta) -> int:
    return int(value.total_seconds())


SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        **TARGET_FIELDS,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ALL_TVS, default=False): cv.boolean,
        vol.Required("title"): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("message"): cv.string,
        vol.Optional("kind"): vol.In(MESSAGE_KINDS),
        vol.Optional("display"): vol.In(MESSAGE_DISPLAYS),
        vol.Optional("expires_in"): vol.All(cv.time_period, _seconds, vol.Range(min=1)),
    }
)
SEND_COMMAND_SCHEMA = vol.Schema(
    {
        **TARGET_FIELDS,
        vol.Required("command"): vol.In(COMMAND_TYPES),
        vol.Optional("payload"): vol.Any(None, dict),
    }
)
REFRESH_CHANNELS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional("wait", default=False): cv.boolean,
    }
)


def _hubs(hass: HomeAssistant) -> dict[str, HotelTvHub]:
    """Loaded entries' hubs by entry id."""
    return {
        entry.entry_id: entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    }


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def resolve_tvs(hass: HomeAssistant, call: ServiceCall) -> list[tuple[HotelTvHub, str]]:
    """The (hub, TV id) pairs a call targets via devices, entities or areas."""
    hubs = _hubs(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device_ids: list[str] = _as_list(call.data.get(ATTR_DEVICE_ID))
    for entity_id in _as_list(call.data.get(ATTR_ENTITY_ID)):
        entry = ent_reg.async_get(entity_id)
        if entry is None or entry.device_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_tv",
                translation_placeholders={"target": entity_id},
            )
        device_ids.append(entry.device_id)
    for area_id in _as_list(call.data.get(ATTR_AREA_ID)):
        device_ids.extend(d.id for d in dr.async_entries_for_area(dev_reg, area_id))

    found: dict[tuple[str, str], tuple[HotelTvHub, str]] = {}
    for device_id in dict.fromkeys(device_ids):
        device = dev_reg.async_get(device_id)
        match = None
        if device is not None:
            for entry_id in device.config_entries:
                hub = hubs.get(entry_id)
                if hub is None:
                    continue
                for domain, ident in device.identifiers:
                    if domain == DOMAIN and ident in hub.tvs:
                        match = (hub, ident)
        if match is None:
            if call.data.get(ATTR_AREA_ID) and device_id not in _as_list(
                call.data.get(ATTR_DEVICE_ID)
            ):
                continue  # other devices of an area are fine to skip
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_tv",
                translation_placeholders={"target": device_id},
            )
        found[(match[0].entry.entry_id, match[1])] = match
    return list(found.values())


def _require_tvs(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[HotelTvHub, str]]:
    tvs = resolve_tvs(hass, call)
    if not tvs:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_tvs"
        )
    return tvs


def _selected_hubs(hass: HomeAssistant, call: ServiceCall) -> list[HotelTvHub]:
    hubs = _hubs(hass)
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if entry_id:
        if entry_id not in hubs:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"entry": entry_id},
            )
        return [hubs[entry_id]]
    if not hubs:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entries"
        )
    return list(hubs.values())


async def _api(hub: HotelTvHub, coro: Awaitable[Any]) -> Any:
    try:
        return await coro
    except HotelTvAuthError as err:
        hub.async_start_reauth()
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except HotelTvError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": str(err)},
        ) from err


async def _send_message(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    body: dict[str, Any] = {"title": call.data["title"]}
    if call.data.get("message"):
        body["body"] = call.data["message"]
    for key in ("kind", "display", "expires_in"):
        if call.data.get(key) is not None:
            body[key] = call.data[key]

    targeted = any(
        call.data.get(k) for k in (ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_AREA_ID)
    )
    results: list[dict[str, Any]] = []
    if targeted:
        for hub, tv_id in _require_tvs(hass, call):
            message_id = await _api(
                hub, hub.client.async_send_message({**body, "device_id": tv_id})
            )
            results.append({"tv_id": tv_id, "message_id": message_id})
    elif call.data[ATTR_ALL_TVS]:
        for hub in _selected_hubs(hass, call):
            message_id = await _api(
                hub, hub.client.async_send_message({**body, "device_id": None})
            )
            results.append(
                {
                    "property_id": hub.property_id,
                    "tv_id": None,
                    "message_id": message_id,
                }
            )
    else:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_tvs"
        )
    return {"messages": results}


async def _send_command(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    results = []
    for hub, tv_id in _require_tvs(hass, call):
        await _api(
            hub,
            hub.client.async_command(
                tv_id, call.data["command"], call.data.get("payload") or None
            ),
        )
        results.append({"tv_id": tv_id, "tv_name": hub.tv_name(tv_id)})
    return {"tvs": results}


async def _refresh_channels(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    results = []
    for hub in _selected_hubs(hass, call):
        answer = await _api(
            hub, hub.client.async_refresh_channels(wait=call.data["wait"])
        )
        results.append({"property_id": hub.property_id, **answer})
    return {"properties": results}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services."""

    def wrap(
        handler: Callable[[HomeAssistant, ServiceCall], Awaitable[Any]],
    ) -> Callable[[ServiceCall], Awaitable[Any]]:
        async def _handle(call: ServiceCall) -> Any:
            return await handler(hass, call)

        return _handle

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        wrap(_send_message),
        schema=SEND_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        wrap(_send_command),
        schema=SEND_COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_CHANNELS,
        wrap(_refresh_channels),
        schema=REFRESH_CHANNELS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
