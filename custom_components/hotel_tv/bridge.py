"""Room controls bridge: guests control the room's Home Assistant entities from the TV.

The admin chooses, per room, which entities are exposed (options flow). For those
only, the bridge
  (a) PUTs the room's control definitions to the server,
  (b) PATCHes their state when the entities change (debounced),
  (c) executes `control.requested` events from the TV and acks them.
Nothing that was not exposed for that very room is ever touched.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import HotelTvAuthError, HotelTvError
from .const import (
    CONTROL_DEBOUNCE,
    CONTROL_REQUEST_MAX_AGE,
    OPT_CONTROLS,
    SERVICE_CALL_TIMEOUT,
)
from .controls import (
    CLIMATE_SENSOR_CAPABILITIES,
    ControlRequestError,
    build_control,
    control_kind,
    remembered_hvac_mode,
    service_call,
)

if TYPE_CHECKING:
    from .hub import HotelTvHub

_LOGGER = logging.getLogger(__name__)

# Fields whose change needs a full PUT (a PATCH carries only state/available).
_STRUCTURAL = ("kind", "label", "icon", "position", "capabilities")


def parse_mapping(options: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """{room_id: {entity_id: label override}} from the entry options, cleaned."""
    raw = options.get(OPT_CONTROLS) or {}
    mapping: dict[str, dict[str, str]] = {}
    if not isinstance(raw, Mapping):
        return mapping
    for room_id, entities in raw.items():
        if not isinstance(entities, Mapping):
            continue
        clean = {
            str(entity_id): str(label or "")
            for entity_id, label in entities.items()
            if control_kind(str(entity_id)) is not None
        }
        if clean:
            mapping[str(room_id)] = clean
    return mapping


class ControlsBridge:
    """Exposes chosen entities of each room to its TV."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, hub: HotelTvHub
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.hub = hub
        self.mapping = parse_mapping(entry.options)
        # What the server has, per room and key (the definitions last sent).
        self.sent: dict[str, dict[str, dict[str, Any]]] = {}
        self._dirty: dict[str, set[str]] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._unsub_state: CALLBACK_TYPE | None = None
        self._handled: deque[str] = deque(maxlen=200)
        # Thermostats: the last mode other than `off` each was seen in (turn_on).
        self._last_hvac_mode: dict[str, str] = {}
        # Thermostats: "measures temperature / humidity", once seen. The attribute
        # comes and goes with its value; the capability must not follow it.
        self._measured: dict[str, set[str]] = {}
        self._started = False
        self.last_error: str | None = None

    # -- lifecycle -------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Sync once Home Assistant has started (other integrations' entities exist)."""
        self.entry.async_on_unload(async_at_started(self.hass, self._async_ha_started))

    async def _async_ha_started(self, _hass: HomeAssistant) -> None:
        self._started = True
        self._track()
        for room_id in list(self.mapping):
            await self._async_put_room(room_id)
        await self.async_process_pending()

    async def async_process_pending(self) -> None:
        """Handle guest requests made while we were not listening.

        Each goes through the same path as a live `control.requested` event (same
        exposure, validation and freshness checks); ids already handled are skipped.
        """
        if not self._started:
            return  # _async_ha_started calls this once HA is up
        try:
            pending = await self.hub.client.async_get_pending_requests()
        except HotelTvAuthError:
            self.hub.async_start_reauth()
            return
        except HotelTvError as err:
            _LOGGER.debug("Could not read pending control requests: %s", err)
            return
        for request in pending:
            if request.get("id") is None:
                continue
            await self.async_handle_request(
                {
                    "type": "control.requested",
                    "room_id": request.get("room_id"),
                    "payload": {
                        "request_id": request["id"],
                        "control_id": request.get("control_id"),
                        "key": request.get("key"),
                        "action": request.get("action"),
                        "params": request.get("params") or {},
                    },
                    "created_at": request.get("created_at"),
                }
            )

    @callback
    def async_stop(self) -> None:
        """Unload."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    async def async_options_updated(self, options: Mapping[str, Any]) -> None:
        """The admin changed the exposed entities: PUT the rooms that changed."""
        old = self.mapping
        self.mapping = parse_mapping(options)
        if not self._started:
            return
        self._track()
        for room_id in sorted(set(old) | set(self.mapping)):
            if old.get(room_id) != self.mapping.get(room_id):
                await self._async_put_room(room_id)

    def exposed(self, room_id: str | None, entity_id: str) -> bool:
        """The entity was explicitly exposed for this room."""
        return bool(room_id) and entity_id in self.mapping.get(str(room_id), {})

    # -- HA → server -------------------------------------------------------------

    @callback
    def _track(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        entity_ids = sorted({e for entities in self.mapping.values() for e in entities})
        for memory in (self._last_hvac_mode, self._measured):
            for entity_id in set(memory) - set(entity_ids):
                del memory[entity_id]
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            self._remember_mode(entity_id, state.state if state else None)
        if entity_ids:
            self._unsub_state = async_track_state_change_event(
                self.hass, entity_ids, self._async_state_changed
            )

    @callback
    def _remember_mode(self, entity_id: str, state: str | None) -> None:
        if control_kind(entity_id) != "climate":
            return
        mode = remembered_hvac_mode(state)
        if mode is not None:
            self._last_hvac_mode[entity_id] = mode

    def _latch_measured(self, control: dict[str, Any]) -> None:
        """Keep `current_temperature` / `current_humidity` once the entity had them.

        Home Assistant leaves `current_humidity` out (and nulls
        `current_temperature`) while the value is unknown — e.g. a thermostat that
        is off. Following that would re-send the room's definitions every time.
        """
        seen = self._measured.setdefault(control["key"], set())
        capabilities = control["capabilities"]
        for name in CLIMATE_SENSOR_CAPABILITIES:
            if capabilities.get(name):
                seen.add(name)
            elif name in seen:
                capabilities[name] = True

    def definitions(self, room_id: str) -> list[dict[str, Any]]:
        """The room's control definitions from the entities' current state."""
        result = []
        sent = self.sent.get(room_id, {})
        unit = str(self.hass.config.units.temperature_unit)
        for position, (entity_id, label) in enumerate(
            self.mapping.get(room_id, {}).items()
        ):
            state = self.hass.states.get(entity_id)
            control = build_control(
                entity_id,
                state.state if state else None,
                state.attributes if state else None,
                label=label,
                position=position,
                unit=unit,
            )
            if control["kind"] == "climate" and control["available"]:
                self._latch_measured(control)
            previous = sent.get(entity_id)
            if not control["available"] and previous is not None:
                # An unavailable entity has lost its attributes: keep what the TV
                # knows (label, limits, last state) and only mark it unavailable.
                control = {
                    **previous,
                    "position": position,
                    "label": control["label"] if label else previous["label"],
                    "available": False,
                }
                if control["state"] is None:
                    control["state"] = build_control(entity_id, None, None)["state"]
            result.append(control)
        return result

    async def _async_put_room(self, room_id: str) -> None:
        if room_id not in self.hub.rooms:
            _LOGGER.debug("Room %s is not on the server; controls not sent", room_id)
            self.sent.pop(room_id, None)
            return
        controls = self.definitions(room_id)
        try:
            await self.hub.client.async_put_controls(room_id, controls)
        except HotelTvAuthError:
            self.hub.async_start_reauth()
            self.sent.pop(room_id, None)
            return
        except HotelTvError as err:
            self.last_error = str(err)
            _LOGGER.warning(
                "Could not send the controls of room %s: %s",
                self.hub.room_name(room_id),
                err,
            )
            # Unknown server state: the next change sends everything again.
            self.sent.pop(room_id, None)
            return
        self.sent[room_id] = {c["key"]: c for c in controls}

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        self._remember_mode(entity_id, new_state.state if new_state else None)
        for room_id, entities in self.mapping.items():
            if entity_id in entities:
                self._mark_dirty(room_id, entity_id)

    @callback
    def _mark_dirty(self, room_id: str, entity_id: str) -> None:
        self._dirty.setdefault(room_id, set()).add(entity_id)
        if room_id in self._timers:
            return

        @callback
        def _fire(_now: Any) -> None:
            self._timers.pop(room_id, None)
            self.entry.async_create_task(
                self.hass, self.async_flush(room_id), f"hotel_tv flush {room_id}"
            )

        self._timers[room_id] = async_call_later(self.hass, CONTROL_DEBOUNCE, _fire)

    async def async_flush(self, room_id: str) -> None:
        """Push the room's changed controls: PATCH state, or PUT when the shape changed."""
        keys = self._dirty.pop(room_id, set())
        if room_id not in self.mapping:
            return
        sent = self.sent.get(room_id)
        current = {c["key"]: c for c in self.definitions(room_id)}
        if (
            sent is None
            or set(sent) != set(current)
            or any(
                sent[key].get(f) != current[key].get(f)
                for key in current
                for f in _STRUCTURAL
            )
        ):
            await self._async_put_room(room_id)
            return
        for key in sorted(keys):
            new = current.get(key)
            old = sent.get(key)
            if new is None or old is None:
                continue
            if new["state"] == old["state"] and new["available"] == old["available"]:
                continue
            try:
                await self.hub.client.async_patch_control(
                    room_id, key, {"state": new["state"], "available": new["available"]}
                )
            except HotelTvAuthError:
                self.hub.async_start_reauth()
                return
            except HotelTvError as err:
                self.last_error = str(err)
                _LOGGER.debug("Could not update control %s: %s", key, err)
                # The server's copy is unknown now: send it all next time.
                self.sent.pop(room_id, None)
                return
            sent[key] = new

    # -- server → HA ---------------------------------------------------------------

    async def async_handle_request(self, event: Mapping[str, Any]) -> None:
        """A guest pressed something on the TV (`control.requested`)."""
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        request_id = payload.get("request_id")
        if request_id is None or request_id == "":
            return
        request_key = str(request_id)
        if request_key in self._handled:
            return
        self._handled.append(request_key)

        room_id = str(event.get("room_id") or "")
        key = str(payload.get("key") or "")
        action = str(payload.get("action") or "")
        params = payload.get("params")

        status = await self._async_execute(event, room_id, key, action, params)
        try:
            await self.hub.client.async_ack(request_id, status)
        except HotelTvAuthError:
            self.hub.async_start_reauth()
        except HotelTvError as err:
            _LOGGER.warning(
                "Could not acknowledge control request %s: %s", request_id, err
            )

        if self.exposed(room_id, key):
            # The server applied the guest's wish optimistically; send the truth.
            sent = self.sent.get(room_id, {}).get(key)
            if sent is not None:
                sent["state"] = None
            self._mark_dirty(room_id, key)

    async def _async_execute(
        self,
        event: Mapping[str, Any],
        room_id: str,
        key: str,
        action: str,
        params: Any,
    ) -> str:
        if not self.exposed(room_id, key):
            _LOGGER.warning(
                "Refused a TV request for %s in room %s: not exposed for that room",
                key or "?",
                self.hub.room_name(room_id) if room_id else "?",
            )
            return "failed"
        created = dt_util.parse_datetime(str(event.get("created_at") or ""))
        if created is not None and (
            (dt_util.utcnow() - created).total_seconds() > CONTROL_REQUEST_MAX_AGE
        ):
            _LOGGER.info("Ignored a stale TV request for %s (%s)", key, created)
            return "failed"
        state = self.hass.states.get(key)
        if state is None or state.state == "unavailable":
            return "failed"
        try:
            call = service_call(
                key,
                action,
                params,
                state.attributes,
                last_hvac_mode=self._last_hvac_mode.get(key),
            )
        except ControlRequestError as err:
            _LOGGER.info("Refused a TV request for %s: %s", key, err)
            return "failed"
        try:
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT):
                await self.hass.services.async_call(
                    call.domain,
                    call.service,
                    call.data,
                    blocking=True,
                    context=Context(),
                )
        except (HomeAssistantError, vol.Invalid, TimeoutError) as err:
            _LOGGER.warning(
                "TV request %s.%s for %s failed: %s",
                call.domain,
                call.service,
                key,
                err,
            )
            return "failed"
        return "done"
