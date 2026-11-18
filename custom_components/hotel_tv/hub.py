"""The runtime of one Hotel TV property (one config entry).

Holds rooms, channels and packages in memory, keeps them current from the server's
event stream (`GET /api/v1/events`, SSE) and a periodic full refresh, and tells the
entities through dispatcher signals.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import (
    HotelTvAuthError,
    HotelTvClient,
    HotelTvError,
    HotelTvNotFoundError,
)
from .const import (
    CONF_PROPERTY_ID,
    CONF_PROPERTY_NAME,
    DOMAIN,
    EVENT_HOTEL_TV,
    REFRESH_INTERVAL,
    SSE_BACKOFF_MAX,
    SSE_BACKOFF_MIN,
    SSE_READ_TIMEOUT,
)
from .sse import SseEvent

if TYPE_CHECKING:
    from .bridge import ControlsBridge

_LOGGER = logging.getLogger(__name__)

DEVICE_STATE_FIELDS = (
    "power",
    "channel_id",
    "channel_number",
    "channel_name",
    "volume",
    "muted",
)
ROOM_EVENT_FIELDS = ("guest_name", "occupied", "package_id")


def find_device(
    registry: dr.DeviceRegistry, entry_id: str, identifier: str
) -> dr.DeviceEntry | None:
    """The entry's device with (DOMAIN, identifier), across HA versions.

    HA 2026.x looks devices up per config entry (`async_get_device_by_identifier`,
    child devices separately); older versions only have `async_get_device`.
    """
    ident = (DOMAIN, identifier)
    if hasattr(registry, "async_get_device_by_identifier"):
        device = registry.async_get_device_by_identifier(ident, entry_id)
        if device is None and hasattr(registry, "async_get_child_device_by_identifier"):
            device = registry.async_get_child_device_by_identifier(ident, entry_id)
        return device
    return registry.async_get_device(identifiers={ident})


def signal_room(entry_id: str, room_id: str) -> str:
    """Dispatcher signal: one room changed."""
    return f"{DOMAIN}_{entry_id}_room_{room_id}"


def signal_update(entry_id: str) -> str:
    """Dispatcher signal: hub availability, channels or packages changed."""
    return f"{DOMAIN}_{entry_id}_update"


def signal_new_entities(entry_id: str) -> str:
    """Dispatcher signal: rooms appeared or got their first TV; platforms add entities."""
    return f"{DOMAIN}_{entry_id}_new_entities"


class HotelTvHub:
    """Rooms, channels and packages of one property, kept live."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: HotelTvClient,
        info: dict[str, Any],
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.info = info
        self.property_id: str = entry.data[CONF_PROPERTY_ID]
        self.property_name: str = str(
            (info.get("property") or {}).get("name")
            or entry.data.get(CONF_PROPERTY_NAME)
            or "Hotel TV"
        )
        self.rooms: dict[str, dict[str, Any]] = {}
        self.channels: list[dict[str, Any]] = []
        self.packages: list[dict[str, Any]] = []
        # The server answered the last full refresh.
        self.available = True
        self.stream_connected = False
        self.last_event_id: str | None = None
        self.last_event_at: str | None = None
        self.last_refresh_at: str | None = None
        self.bridge: ControlsBridge | None = None
        self.hub_device_id: str | None = None
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._reauth_started = False
        self._refresh_lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------------

    async def async_load(self) -> None:
        """First full load; errors propagate (setup turns them into NotReady)."""
        rooms, channels, packages = await asyncio.gather(
            self.client.async_get_rooms(),
            self.client.async_get_channels(),
            self.client.async_get_packages(),
        )
        self.channels = channels
        self.packages = packages
        self.rooms = {str(r["id"]): r for r in rooms if r.get("id")}
        self.last_refresh_at = dt_util.utcnow().isoformat()
        self._update_hub_device()

    @callback
    def async_start(self) -> None:
        """Start the event stream and the safety-net refresh."""
        self.entry.async_create_background_task(
            self.hass, self._async_run_events(), f"{DOMAIN} events {self.property_id}"
        )
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._async_periodic_refresh,
                timedelta(seconds=REFRESH_INTERVAL),
                name=f"{DOMAIN} refresh",
            )
        )

    async def async_unload(self) -> None:
        """Stop."""
        self._stopping = True
        self._stop_event.set()
        if self.bridge is not None:
            self.bridge.async_stop()

    # -- helpers ----------------------------------------------------------------

    def room_name(self, room_id: str) -> str:
        """The room's name ("204")."""
        return str((self.rooms.get(room_id) or {}).get("name") or room_id)

    def device_name(self, room_id: str) -> str:
        """The HA device name of a room."""
        return f"{self.property_name} · {self.room_name(room_id)}"

    def tv(self, room_id: str) -> dict[str, Any] | None:
        """The room's first TV device (the one entities follow)."""
        devices = (self.rooms.get(room_id) or {}).get("devices") or []
        return devices[0] if devices else None

    def room_of_device(self, device_id: str) -> str | None:
        """The room a TV device is in."""
        for room_id, room in self.rooms.items():
            if any(d.get("id") == device_id for d in room.get("devices") or []):
                return room_id
        return None

    def room_channels(self, room_id: str) -> list[dict[str, Any]]:
        """The enabled channels of the room's package (own or default), in its order.

        Falls back to all enabled channels, by number, when the package or its
        channel list is unknown.
        """
        package_id = (self.rooms.get(room_id) or {}).get("package_id")
        package = next(
            (
                p
                for p in self.packages
                if (p.get("id") == package_id if package_id else p.get("is_default"))
            ),
            None,
        )
        enabled = {c.get("id"): c for c in self.channels if c.get("enabled", True)}
        ids = package.get("channel_ids") if package else None
        if isinstance(ids, list):
            return [enabled[i] for i in ids if i in enabled]
        return sorted(
            enabled.values(),
            key=lambda c: (c.get("number") is None, c.get("number") or 0),
        )

    def channel_by_id(self, channel_id: str | None) -> dict[str, Any] | None:
        """A channel of the property."""
        return next((c for c in self.channels if c.get("id") == channel_id), None)

    def ha_device_id(self, room_id: str | None) -> str | None:
        """The HA device registry id of a room."""
        if not room_id:
            return None
        device = find_device(dr.async_get(self.hass), self.entry.entry_id, room_id)
        return device.id if device else None

    @callback
    def _signal_room(self, room_id: str) -> None:
        async_dispatcher_send(self.hass, signal_room(self.entry.entry_id, room_id))

    @callback
    def _signal_update(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    @callback
    def async_start_reauth(self) -> None:
        """The token was refused."""
        if not self._reauth_started:
            self._reauth_started = True
            _LOGGER.warning("Hotel TV refused the API token; reauthentication needed")
            self.entry.async_start_reauth(self.hass)

    # -- full refresh ---------------------------------------------------------

    async def _async_periodic_refresh(self, _now: Any = None) -> None:
        await self.async_refresh()

    async def async_refresh(self) -> bool:
        """Re-read rooms, channels and packages. False when the server is unreachable."""
        async with self._refresh_lock:
            try:
                rooms, channels, packages = await asyncio.gather(
                    self.client.async_get_rooms(),
                    self.client.async_get_channels(),
                    self.client.async_get_packages(),
                )
            except HotelTvAuthError:
                self._set_available(False)
                self.async_start_reauth()
                return False
            except HotelTvError as err:
                if self.available:
                    _LOGGER.warning("Hotel TV is unreachable: %s", err)
                self._set_available(False)
                return False
            if not self.available:
                _LOGGER.info("Hotel TV is reachable again")
            self.last_refresh_at = dt_util.utcnow().isoformat()
            changed = channels != self.channels or packages != self.packages
            self.channels = channels
            self.packages = packages
            self.apply_rooms(rooms)
            self._set_available(True, force_signal=changed)
            return True

    @callback
    def _set_available(self, available: bool, *, force_signal: bool = False) -> None:
        if available != self.available or force_signal:
            self.available = available
            self._signal_update()

    @callback
    def apply_rooms(self, rooms: list[dict[str, Any]]) -> None:
        """Replace the room list (full refresh)."""
        fresh = {str(r["id"]): r for r in rooms if r.get("id")}
        for room_id in list(self.rooms):
            if room_id not in fresh:
                del self.rooms[room_id]
                self._remove_device(room_id)
        new_entities = False
        for room_id, room in fresh.items():
            new_entities |= self._store_room(room_id, room)
        if new_entities:
            async_dispatcher_send(self.hass, signal_new_entities(self.entry.entry_id))

    @callback
    def apply_room(self, room: dict[str, Any]) -> None:
        """Store one room answer (GET / PATCH / checkin / checkout)."""
        room_id = str(room.get("id") or "")
        if not room_id:
            return
        if self._store_room(room_id, room):
            async_dispatcher_send(self.hass, signal_new_entities(self.entry.entry_id))

    def _store_room(self, room_id: str, room: dict[str, Any]) -> bool:
        """Store; signal when changed. True when platforms may have entities to add."""
        old = self.rooms.get(room_id)
        self.rooms[room_id] = room
        if old == room:
            return False
        if old is not None and old.get("name") != room.get("name"):
            self._update_room_device(room_id)
        self._signal_room(room_id)
        return old is None or (not old.get("devices") and bool(room.get("devices")))

    def _update_hub_device(self) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.property_id)},
            name=self.property_name,
            manufacturer="Hotel TV",
            model="Hotel TV server",
            sw_version=str(self.info.get("version") or "") or None,
            configuration_url=f"{self.client.url}/admin",
        )
        self.hub_device_id = device.id

    def _update_room_device(self, room_id: str) -> None:
        registry = dr.async_get(self.hass)
        device = find_device(registry, self.entry.entry_id, room_id)
        if device is not None and device.name_by_user is None:
            registry.async_update_device(device.id, name=self.device_name(room_id))

    def _remove_device(self, room_id: str) -> None:
        registry = dr.async_get(self.hass)
        device = find_device(registry, self.entry.entry_id, room_id)
        if device is not None:
            registry.async_update_device(
                device.id, remove_config_entry_id=self.entry.entry_id
            )

    # -- event stream -----------------------------------------------------------

    async def _async_run_events(self) -> None:
        """Keep one SSE connection open; resume with Last-Event-ID, back off on errors."""
        backoff = SSE_BACKOFF_MIN
        opened_before = False

        @callback
        def _opened() -> None:
            nonlocal backoff, opened_before
            self.stream_connected = True
            backoff = SSE_BACKOFF_MIN
            _LOGGER.debug(
                "Hotel TV event stream connected (cursor %s)", self.last_event_id
            )
            if opened_before:
                # The resume replays what was missed; this also catches anything else.
                self.hass.async_create_task(self.async_refresh())
            opened_before = True
            if self.bridge is not None:
                # Guest requests made while the stream was down.
                self.hass.async_create_task(self.bridge.async_process_pending())

        while not self._stopping:
            try:
                async for event in self.client.async_events(
                    self.last_event_id,
                    read_timeout=SSE_READ_TIMEOUT,
                    on_open=_opened,
                ):
                    await self.async_handle_sse(event)
            except HotelTvAuthError:
                self.stream_connected = False
                self.async_start_reauth()
                return
            except HotelTvError as err:
                _LOGGER.debug("Hotel TV event stream broke: %s", err)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Unexpected error in the Hotel TV event stream")
            self.stream_connected = False
            if self._stopping:
                return
            # Is the server itself gone? Entities follow.
            await self.async_refresh()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), backoff)
            backoff = min(max(backoff, 0.5) * 2, SSE_BACKOFF_MAX)

    async def async_handle_sse(self, sse: SseEvent) -> None:
        """One SSE message from /events."""
        if sse.event == "ping":
            return
        if sse.id:
            self.last_event_id = sse.id
        try:
            event = json.loads(sse.data)
        except ValueError:
            _LOGGER.debug("Ignoring a malformed Hotel TV event: %s", sse.data[:200])
            return
        if not isinstance(event, dict):
            return
        event.setdefault("type", sse.event)
        if sse.id:
            event.setdefault("id", sse.id)
        await self.async_handle_event(event)

    async def async_handle_event(self, event: dict[str, Any]) -> None:
        """Apply one integration event and fire it on the HA bus."""
        etype = str(event.get("type") or "")
        room_id = event.get("room_id")
        device_id = event.get("device_id")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        self.last_event_at = dt_util.utcnow().isoformat()

        if device_id and not room_id:
            room_id = self.room_of_device(device_id)

        if etype == "device.state":
            self._apply_device(
                room_id,
                device_id,
                {k: payload[k] for k in DEVICE_STATE_FIELDS if k in payload},
            )
        elif etype in ("device.online", "device.offline"):
            self._apply_device(room_id, device_id, {"online": etype == "device.online"})
        elif etype == "room.updated":
            await self._async_room_updated(room_id, payload)
        elif etype == "channels.updated":
            await self._async_channels_updated()
        elif etype == "control.requested" and self.bridge is not None:
            self.hass.async_create_task(
                self.bridge.async_handle_request(event),
                f"{DOMAIN} control request",
            )

        self.hass.bus.async_fire(
            EVENT_HOTEL_TV,
            {
                "config_entry_id": self.entry.entry_id,
                "property_id": self.property_id,
                "id": event.get("id"),
                "type": etype,
                "room_id": room_id,
                "room_name": self.room_name(room_id) if room_id else None,
                "device_id": self.ha_device_id(room_id),
                "tv_device_id": device_id,
                "payload": payload,
                "created_at": event.get("created_at"),
            },
        )

    @callback
    def _apply_device(
        self, room_id: str | None, device_id: str | None, changes: dict[str, Any]
    ) -> None:
        room = self.rooms.get(room_id or "")
        devices = (room or {}).get("devices") or []
        device = next((d for d in devices if d.get("id") == device_id), None)
        if room is None or device is None:
            # A device we do not know (just paired / moved): re-read everything.
            self.hass.async_create_task(self.async_refresh())
            return
        device.update(changes)
        if changes.get("online"):
            device["last_seen_at"] = dt_util.utcnow().isoformat()
        if "channel_id" in changes and "channel_name" not in changes:
            channel = self.channel_by_id(changes["channel_id"])
            device["channel_name"] = channel.get("name") if channel else None
            device["channel_number"] = channel.get("number") if channel else None
        self._signal_room(str(room_id))

    async def _async_room_updated(
        self, room_id: str | None, payload: dict[str, Any]
    ) -> None:
        if not room_id:
            await self.async_refresh()
            return
        room = self.rooms.get(room_id)
        if room is not None:
            # Apply what the event carries at once, then the full room.
            room.update({k: payload[k] for k in ROOM_EVENT_FIELDS if k in payload})
            self._signal_room(room_id)
        try:
            fresh = await self.client.async_get_room(room_id)
        except HotelTvNotFoundError:
            if room_id in self.rooms:
                del self.rooms[room_id]
                self._remove_device(room_id)
            return
        except HotelTvAuthError:
            self.async_start_reauth()
            return
        except HotelTvError as err:
            _LOGGER.debug("Could not re-read room %s: %s", room_id, err)
            return
        self.apply_room(fresh)

    async def _async_channels_updated(self) -> None:
        try:
            channels, packages = await asyncio.gather(
                self.client.async_get_channels(), self.client.async_get_packages()
            )
        except HotelTvAuthError:
            self.async_start_reauth()
            return
        except HotelTvError as err:
            _LOGGER.debug("Could not re-read channels: %s", err)
            return
        self.channels = channels
        self.packages = packages
        self._signal_update()
