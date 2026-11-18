"""Services: messages, check-in / check-out, guest details, raw commands, channels.

Rooms are targeted by their Hotel TV device (or any entity / area of it).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
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
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import HotelTvAuthError, HotelTvError
from .const import (
    COMMAND_TYPES,
    DOMAIN,
    MESSAGE_DISPLAYS,
    MESSAGE_KINDS,
    SERVICE_CHECK_IN,
    SERVICE_CHECK_OUT,
    SERVICE_REFRESH_CHANNELS,
    SERVICE_SEND_COMMAND,
    SERVICE_SEND_MESSAGE,
    SERVICE_SET_GUEST,
)

if TYPE_CHECKING:
    from .hub import HotelTvHub

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ALL_ROOMS = "all_rooms"

TARGET_FIELDS = cv.ENTITY_SERVICE_FIELDS


def _iso(value: datetime) -> str:
    return dt_util.as_utc(value).isoformat()


def _seconds(value: timedelta) -> int:
    return int(value.total_seconds())


SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        **TARGET_FIELDS,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ALL_ROOMS, default=False): cv.boolean,
        vol.Required("title"): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("message"): cv.string,
        vol.Optional("kind"): vol.In(MESSAGE_KINDS),
        vol.Optional("display"): vol.In(MESSAGE_DISPLAYS),
        vol.Optional("expires_in"): vol.All(cv.time_period, _seconds, vol.Range(min=1)),
    }
)
CHECK_IN_SCHEMA = vol.Schema(
    {
        **TARGET_FIELDS,
        vol.Required("guest_name"): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("guest_locale"): cv.string,
        vol.Optional("welcome_message"): cv.string,
        vol.Optional("checkout_at"): vol.All(cv.datetime, _iso),
    }
)
CHECK_OUT_SCHEMA = vol.Schema({**TARGET_FIELDS})
SET_GUEST_FIELDS = (
    "guest_name",
    "guest_locale",
    "welcome_message",
    "occupied",
    "checkin_at",
    "checkout_at",
)
SET_GUEST_SCHEMA = vol.Schema(
    {
        **TARGET_FIELDS,
        vol.Optional("guest_name"): vol.Any(None, cv.string),
        vol.Optional("guest_locale"): vol.Any(None, cv.string),
        vol.Optional("welcome_message"): vol.Any(None, cv.string),
        vol.Optional("occupied"): cv.boolean,
        vol.Optional("checkin_at"): vol.Any(None, vol.All(cv.datetime, _iso)),
        vol.Optional("checkout_at"): vol.Any(None, vol.All(cv.datetime, _iso)),
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


def resolve_rooms(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[HotelTvHub, str]]:
    """The (hub, room id) pairs a call targets via devices, entities or areas."""
    hubs = _hubs(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device_ids: list[str] = _as_list(call.data.get(ATTR_DEVICE_ID))
    for entity_id in _as_list(call.data.get(ATTR_ENTITY_ID)):
        entry = ent_reg.async_get(entity_id)
        if entry is None or entry.device_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_room",
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
                    if domain == DOMAIN and ident in hub.rooms:
                        match = (hub, ident)
        if match is None:
            if call.data.get(ATTR_AREA_ID) and device_id not in _as_list(
                call.data.get(ATTR_DEVICE_ID)
            ):
                continue  # other devices of an area are fine to skip
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_room",
                translation_placeholders={"target": device_id},
            )
        found[(match[0].entry.entry_id, match[1])] = match
    return list(found.values())


def _require_rooms(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[HotelTvHub, str]]:
    rooms = resolve_rooms(hass, call)
    if not rooms:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_rooms"
        )
    return rooms


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
        for hub, room_id in _require_rooms(hass, call):
            message_id = await _api(
                hub, hub.client.async_send_message({**body, "room_id": room_id})
            )
            results.append({"room_id": room_id, "message_id": message_id})
    elif call.data[ATTR_ALL_ROOMS]:
        for hub in _selected_hubs(hass, call):
            message_id = await _api(
                hub, hub.client.async_send_message({**body, "room_id": None})
            )
            results.append(
                {
                    "property_id": hub.property_id,
                    "room_id": None,
                    "message_id": message_id,
                }
            )
    else:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_rooms"
        )
    return {"messages": results}


async def _check_in(hass: HomeAssistant, call: ServiceCall) -> None:
    body = {
        k: call.data[k]
        for k in ("guest_name", "guest_locale", "welcome_message", "checkout_at")
        if call.data.get(k) not in (None, "")
    }
    for hub, room_id in _require_rooms(hass, call):
        hub.apply_room(await _api(hub, hub.client.async_check_in(room_id, body)))


async def _check_out(hass: HomeAssistant, call: ServiceCall) -> None:
    for hub, room_id in _require_rooms(hass, call):
        hub.apply_room(await _api(hub, hub.client.async_check_out(room_id)))


async def _set_guest(hass: HomeAssistant, call: ServiceCall) -> None:
    changes = {
        k: (None if call.data[k] == "" else call.data[k])
        for k in SET_GUEST_FIELDS
        if k in call.data
    }
    if not changes:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="nothing_to_change"
        )
    for hub, room_id in _require_rooms(hass, call):
        hub.apply_room(await _api(hub, hub.client.async_update_room(room_id, changes)))


async def _send_command(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    results = []
    for hub, room_id in _require_rooms(hass, call):
        devices = await _api(
            hub,
            hub.client.async_command(
                room_id, call.data["command"], call.data.get("payload") or None
            ),
        )
        results.append({"room_id": room_id, "devices": devices})
    return {"rooms": results}


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
        DOMAIN, SERVICE_CHECK_IN, wrap(_check_in), schema=CHECK_IN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CHECK_OUT, wrap(_check_out), schema=CHECK_OUT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_GUEST, wrap(_set_guest), schema=SET_GUEST_SCHEMA
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
