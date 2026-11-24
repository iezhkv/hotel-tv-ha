"""The runtime of one Hotel TV property (one config entry).

Holds the TVs, channels and packages in memory, keeps them current from the server's
event stream (`GET /api/v1/events`, SSE) and a periodic full refresh, and tells the
entities through dispatcher signals.

What the server last said is also kept on disk (`.storage/hotel_tv.<entry id>`), so
that Home Assistant can start without the server — after a restart during an outage,
or years after the server is gone: the TVs it listed as paired and the entities bound
to them stay known, and the TVs keep driving their devices through the webhook
(docs/HA-PAIRING.md §4). Everything that needs the server is unavailable until it
answers again; nothing is forgotten for not hearing from it.
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
from homeassistant.helpers.storage import Store
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
    from .wire import WireHandler

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# What the server last said is written this long after a change (seconds).
SAVE_DELAY = 10


def snapshot_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Where a property's last known TVs, channels and packages are kept."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")


async def async_read_snapshot(
    hass: HomeAssistant, entry_id: str
) -> dict[str, Any] | None:
    """The last thing the server said, or None when nothing was ever saved."""
    data = await snapshot_store(hass, entry_id).async_load()
    return data if isinstance(data, dict) and isinstance(data.get("tvs"), dict) else None

DEVICE_STATE_FIELDS = (
    "power",
    "channel_id",
    "channel_number",
    "channel_name",
    "volume",
    "muted",
)
TV_EVENT_FIELDS = ("name", "package_id")
# The HA device model of a TV, by its kind.
TV_MODELS = {"android": "Android TV", "linux": "Linux TV"}


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


def signal_tv(entry_id: str, tv_id: str) -> str:
    """Dispatcher signal: one TV changed."""
    return f"{DOMAIN}_{entry_id}_tv_{tv_id}"


def signal_update(entry_id: str) -> str:
    """Dispatcher signal: hub availability, channels or packages changed."""
    return f"{DOMAIN}_{entry_id}_update"


def signal_new_entities(entry_id: str) -> str:
    """Dispatcher signal: TVs appeared; platforms add their entities."""
    return f"{DOMAIN}_{entry_id}_new_entities"


class HotelTvHub:
    """TVs, channels and packages of one property, kept live."""

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
        # The server's Device objects by id.
        self.tvs: dict[str, dict[str, Any]] = {}
        self.channels: list[dict[str, Any]] = []
        self.packages: list[dict[str, Any]] = []
        # The server answered the last full refresh.
        self.available = True
        self.stream_connected = False
        self.last_event_id: str | None = None
        self.last_event_at: str | None = None
        self.last_refresh_at: str | None = None
        self.bridge: ControlsBridge | None = None
        self.wire: WireHandler | None = None
        self.hub_device_id: str | None = None
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._reauth_started = False
        self._refresh_lock = asyncio.Lock()
        self._store = snapshot_store(hass, entry.entry_id)

    # -- lifecycle ------------------------------------------------------------

    async def async_load(self) -> None:
        """First full load; errors propagate (setup turns them into NotReady)."""
        tvs, channels, packages = await asyncio.gather(
            self.client.async_get_tvs(),
            self.client.async_get_channels(),
            self.client.async_get_packages(),
        )
        self.channels = channels
        self.packages = packages
        self.tvs = {str(t["id"]): t for t in tvs if t.get("id")}
        self.last_refresh_at = dt_util.utcnow().isoformat()
        self._update_hub_device()
        self._remove_stale_devices()
        self.async_schedule_save()

    @callback
    def async_restore(self, snapshot: dict[str, Any]) -> None:
        """Start from what the server last said: it does not answer now.

        Nothing is removed for not being confirmed (no stale-device sweep): the
        server is only silent. The first refresh that reaches it puts things right.
        """
        self.tvs = {str(k): v for k, v in snapshot["tvs"].items() if isinstance(v, dict)}
        channels, packages = snapshot.get("channels"), snapshot.get("packages")
        self.channels = channels if isinstance(channels, list) else []
        self.packages = packages if isinstance(packages, list) else []
        self.last_refresh_at = snapshot.get("saved_at")
        self.available = False
        self._update_hub_device()

    @callback
    def async_schedule_save(self) -> None:
        """Keep what the server said (never called while starting from the disk)."""
        self._store.async_delay_save(self._snapshot, SAVE_DELAY)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "info": self.info,
            "tvs": self.tvs,
            "channels": self.channels,
            "packages": self.packages,
            "saved_at": dt_util.utcnow().isoformat(),
        }

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

    def tv_name(self, tv_id: str) -> str:
        """The TV's name ("TV 204")."""
        return str((self.tvs.get(tv_id) or {}).get("name") or tv_id)

    def device_name(self, tv_id: str) -> str:
        """The HA device name of a TV."""
        return f"{self.property_name} · {self.tv_name(tv_id)}"

    def tv_model(self, tv_id: str) -> str:
        """The HA device model of a TV ("Android TV")."""
        kind = str((self.tvs.get(tv_id) or {}).get("kind") or "")
        return TV_MODELS.get(kind, kind or "TV")

    def tv_channels(self, tv_id: str) -> list[dict[str, Any]]:
        """The enabled channels of the TV's package (own or default), in its order.

        Falls back to all enabled channels, by number, when the package or its
        channel list is unknown.
        """
        package_id = (self.tvs.get(tv_id) or {}).get("package_id")
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

    def ha_device_id(self, tv_id: str | None) -> str | None:
        """The HA device registry id of a TV."""
        if not tv_id:
            return None
        device = find_device(dr.async_get(self.hass), self.entry.entry_id, tv_id)
        return device.id if device else None

    @callback
    def _signal_tv(self, tv_id: str) -> None:
        async_dispatcher_send(self.hass, signal_tv(self.entry.entry_id, tv_id))

    @callback
    def _signal_update(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    @callback
    def async_start_reauth(self) -> None:
        """The token was refused."""
        if not self._reauth_started:
            self._reauth_started = True
            _LOGGER.warning(
                "Hotel TV refused the API token; pair again with a new code"
            )
            self.entry.async_start_reauth(self.hass)

    # -- full refresh ---------------------------------------------------------

    async def _async_periodic_refresh(self, _now: Any = None) -> None:
        await self.async_refresh()

    async def async_refresh(self) -> bool:
        """Re-read TVs, channels and packages. False when the server is unreachable."""
        async with self._refresh_lock:
            try:
                tvs, channels, packages = await asyncio.gather(
                    self.client.async_get_tvs(),
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
            self.apply_tvs(tvs)
            self._set_available(True, force_signal=changed)
            self.async_schedule_save()
            return True

    @callback
    def _set_available(self, available: bool, *, force_signal: bool = False) -> None:
        if available != self.available or force_signal:
            self.available = available
            self._signal_update()

    @callback
    def apply_tvs(self, tvs: list[dict[str, Any]]) -> None:
        """Replace the TV list (full refresh)."""
        fresh = {str(t["id"]): t for t in tvs if t.get("id")}
        for tv_id in list(self.tvs):
            if tv_id not in fresh:
                del self.tvs[tv_id]
                self._remove_device(tv_id)
        new_entities = False
        for tv_id, tv in fresh.items():
            new_entities |= self._store_tv(tv_id, tv)
        if new_entities:
            async_dispatcher_send(self.hass, signal_new_entities(self.entry.entry_id))
        self._remap_controls()

    @callback
    def apply_tv(self, tv: dict[str, Any]) -> None:
        """Store one Device answer (GET / PATCH)."""
        tv_id = str(tv.get("id") or "")
        if not tv_id:
            return
        if self._store_tv(tv_id, tv):
            async_dispatcher_send(self.hass, signal_new_entities(self.entry.entry_id))
        self._remap_controls()
        self.async_schedule_save()

    @callback
    def _remap_controls(self) -> None:
        """The TVs' `ha_entities` (server bindings) may have changed."""
        if self.bridge is not None:
            self.entry.async_create_task(
                self.hass, self.bridge.async_remap(), f"{DOMAIN} remap controls"
            )

    def _store_tv(self, tv_id: str, tv: dict[str, Any]) -> bool:
        """Store; signal when changed. True when the TV is new (entities to add)."""
        old = self.tvs.get(tv_id)
        self.tvs[tv_id] = tv
        if old == tv:
            return False
        if old is not None and (
            old.get("name") != tv.get("name") or old.get("kind") != tv.get("kind")
        ):
            self._update_tv_device(tv_id)
        self._signal_tv(tv_id)
        return old is None

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

    def _update_tv_device(self, tv_id: str) -> None:
        registry = dr.async_get(self.hass)
        device = find_device(registry, self.entry.entry_id, tv_id)
        if device is None:
            return
        changes: dict[str, Any] = {"model": self.tv_model(tv_id)}
        if device.name_by_user is None:
            changes["name"] = self.device_name(tv_id)
        registry.async_update_device(device.id, **changes)

    def _remove_device(self, tv_id: str) -> None:
        registry = dr.async_get(self.hass)
        device = find_device(registry, self.entry.entry_id, tv_id)
        if device is not None:
            registry.async_update_device(
                device.id, remove_config_entry_id=self.entry.entry_id
            )

    def _remove_stale_devices(self) -> None:
        """Drop devices the server no longer has (also the rooms of API 1.x)."""
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            idents = {i[1] for i in device.identifiers if i[0] == DOMAIN}
            if idents and not idents & ({self.property_id} | set(self.tvs)):
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
        tv_id = event.get("device_id")
        tv_id = str(tv_id) if tv_id else None
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        self.last_event_at = dt_util.utcnow().isoformat()

        if etype == "device.state":
            self._apply_state(
                tv_id, {k: payload[k] for k in DEVICE_STATE_FIELDS if k in payload}
            )
        elif etype in ("device.online", "device.offline"):
            self._apply_state(tv_id, {"online": etype == "device.online"})
        elif etype == "device.updated":
            await self._async_tv_updated(tv_id, payload)
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
                "device_id": self.ha_device_id(tv_id),
                "tv_id": tv_id,
                "tv_name": self.tv_name(tv_id) if tv_id else None,
                "payload": payload,
                "created_at": event.get("created_at"),
            },
        )

    @callback
    def _apply_state(self, tv_id: str | None, changes: dict[str, Any]) -> None:
        tv = self.tvs.get(tv_id or "")
        if tv is None:
            # A TV we do not know (just added / paired): re-read everything.
            self.hass.async_create_task(self.async_refresh())
            return
        tv.update(changes)
        if changes.get("online"):
            tv["last_seen_at"] = dt_util.utcnow().isoformat()
        if "channel_id" in changes and "channel_name" not in changes:
            channel = self.channel_by_id(changes["channel_id"])
            tv["channel_name"] = channel.get("name") if channel else None
            tv["channel_number"] = channel.get("number") if channel else None
        self._signal_tv(str(tv_id))

    async def _async_tv_updated(
        self, tv_id: str | None, payload: dict[str, Any]
    ) -> None:
        if not tv_id:
            await self.async_refresh()
            return
        tv = self.tvs.get(tv_id)
        if tv is not None:
            # Apply what the event carries at once, then the full TV.
            changes = {k: payload[k] for k in TV_EVENT_FIELDS if k in payload}
            if any(tv.get(k) != v for k, v in changes.items()):
                renamed = "name" in changes and tv.get("name") != changes["name"]
                tv.update(changes)
                if renamed:
                    self._update_tv_device(tv_id)
                self._signal_tv(tv_id)
        try:
            fresh = await self.client.async_get_tv(tv_id)
        except HotelTvNotFoundError:
            if tv_id in self.tvs:
                del self.tvs[tv_id]
                self._remove_device(tv_id)
                self._remap_controls()
                self.async_schedule_save()
            return
        except HotelTvAuthError:
            self.async_start_reauth()
            return
        except HotelTvError as err:
            _LOGGER.debug("Could not re-read TV %s: %s", tv_id, err)
            return
        self.apply_tv(fresh)

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
        self.async_schedule_save()
