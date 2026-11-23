"""The Home Assistant webhook of a paired property (docs/HA-PAIRING.md §2–§4).

Two kinds of callers post to the same webhook:

- the Hotel TV **server**, signed with the connection `secret` (`ping`, `info`,
  `entities`, `states`, `call`);
- a **TV** (Android shell, offline fallback), signed with its own key derived from
  the secret and naming itself in `X-HotelTV-Device` (`tv.controls`, `tv.control`).
  A TV is accepted only when the server lists it as paired, and only for the
  entities synced to that very TV.

Every answer is JSON: `{"ok": true, …}` or `{"ok": false, "error": "<slug>"}`.
HTTP 401 when the signature (or the TV) is refused, 413 for a too big body, 400 for
a body that is not a JSON object; every other answer is 200.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.json import json_dumps
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    CALL_DOMAINS,
    CONF_SECRET,
    HEADER_DEVICE,
    HEADER_ID,
    HEADER_SIGNATURE,
    MAX_STATES,
    MAX_WEBHOOK_BODY,
    READ_DOMAINS,
    SERVICE_CALL_TIMEOUT,
    SIGNATURE_TOLERANCE,
)
from .signing import PATH_WEBHOOK, ReplayGuard, tv_key, verify_signature

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .hub import HotelTvHub

_LOGGER = logging.getLogger(__name__)

# Fields of a TV control in the `tv.controls` answer (§4).
TV_CONTROL_FIELDS = ("key", "kind", "label", "state", "capabilities", "available")
# Service-call targets other than entity ids would reach beyond the named entities.
FORBIDDEN_TARGETS = ("area_id", "device_id", "floor_id", "label_id")


def local_webhook_urls(hass: HomeAssistant, webhook_id: str) -> list[str]:
    """The internal (LAN) then the external URL of the webhook, when configured.

    No cloudhook: the Hotel TV server is in the hotel (§1). Only http(s) URLs.
    """
    path = webhook.async_generate_path(webhook_id)
    urls: list[str] = []
    for kwargs in (
        {"allow_external": False, "allow_cloud": False, "allow_ip": True},
        {"allow_internal": False, "allow_cloud": False},
    ):
        try:
            base = get_url(hass, **kwargs)
        except NoURLAvailableError:
            continue
        if urlsplit(base).scheme in ("http", "https"):
            urls.append(base + path)
    return list(dict.fromkeys(urls))


def _json(payload: Mapping[str, Any], status: int = 200) -> web.Response:
    # Entity attributes may hold datetimes, enums, …: HA's encoder handles them.
    return web.json_response(payload, status=status, dumps=json_dumps)


def _error(slug: str, status: int = 200) -> web.Response:
    return _json({"ok": False, "error": slug}, status)


async def _read_body(request: web.Request) -> bytes | None:
    """The raw body; None when it is too big."""
    length = getattr(request, "content_length", None)
    if length is not None and length > MAX_WEBHOOK_BODY:
        return None
    if isinstance(request, web.Request):
        body = await request.read()
    else:  # a MockRequest
        body = await request.content.read()
    return None if len(body) > MAX_WEBHOOK_BODY else body


def pairing_handler(
    probe_token: str,
) -> Callable[[HomeAssistant, str, web.Request], Awaitable[web.Response]]:
    """The webhook handler while pairing (§1.2).

    The server probes the webhook URLs with a `ping` signed with OUR probe_token
    before it answers /pair. Only a correctly signed ping is answered; anything
    else is refused.
    """

    async def _handle(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        body = await _read_body(request)
        message: Any = None
        if (
            body is not None
            and verify_signature(
                probe_token,
                request.headers.get(HEADER_SIGNATURE),
                request.headers.get(HEADER_ID) or "",
                request.method,
                PATH_WEBHOOK,
                body,
                dt_util.utcnow().timestamp(),
                SIGNATURE_TOLERANCE,
            )
            is not None
        ):
            try:
                message = json.loads(body)
            except ValueError:
                message = None
        if isinstance(message, dict) and message.get("type") == "ping":
            return _json({"ok": True, "nonce": message.get("nonce")})
        return _error("unauthorized", 401)

    return _handle


def _domain(entity_id: str) -> str:
    return entity_id.partition(".")[0]


def _entity_ids(value: Any) -> list[str] | None:
    """`data.entity_id` as a list of entity ids; None when it is not one."""
    ids = [value] if isinstance(value, str) else value
    if not isinstance(ids, list) or not ids:
        return None
    if not all(isinstance(i, str) and "." in i for i in ids):
        return None
    return [i.strip() for i in ids]


class WireHandler:
    """Verifies and answers the messages posted to the property's webhook."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, hub: HotelTvHub
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.hub = hub
        self.replay = ReplayGuard(SIGNATURE_TOLERANCE)
        self.last_message_at: str | None = None

    @property
    def _secret(self) -> str:
        return str(self.entry.data.get(CONF_SECRET) or "")

    # -- entry point -----------------------------------------------------------

    async def async_handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Verify, refuse replays and dispatch one message."""
        body = await _read_body(request)
        if body is None:
            return _error("too_large", 413)
        headers = request.headers
        message_id = headers.get(HEADER_ID) or ""
        device_id = (headers.get(HEADER_DEVICE) or "").strip()
        secret = self._secret
        key = tv_key(secret, device_id) if device_id and secret else secret
        now = dt_util.utcnow().timestamp()
        signed_t = verify_signature(
            key,
            headers.get(HEADER_SIGNATURE),
            message_id,
            request.method,
            PATH_WEBHOOK,
            body,
            now,
            SIGNATURE_TOLERANCE,
        )
        if signed_t is None:
            return _error("unauthorized", 401)
        if device_id and not self._known_tv(device_id):
            return _error("unknown_device", 401)
        if not self.replay.check_and_remember(message_id, signed_t, now):
            return _error("replay")
        try:
            message = json.loads(body)
        except ValueError:
            return _error("bad_request", 400)
        if not isinstance(message, dict):
            return _error("bad_request", 400)
        self.last_message_at = dt_util.utcnow().isoformat()
        try:
            if device_id:
                answer = await self._async_tv_message(device_id, message)
            else:
                answer = await self._async_server_message(message)
        except Exception:
            _LOGGER.exception(
                "Hotel TV: error handling a %s message", message.get("type")
            )
            return _error("internal_error", 500)
        return _json(answer)

    def _known_tv(self, device_id: str) -> bool:
        """A TV of this property that the server lists as paired."""
        tv = self.hub.tvs.get(device_id)
        return tv is not None and bool(tv.get("paired"))

    # -- server → HA (§2) ------------------------------------------------------

    async def _async_server_message(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = message.get("type")
        if kind == "ping":
            return {"ok": True, "nonce": message.get("nonce")}
        if kind == "info":
            return {
                "ok": True,
                "version": HA_VERSION,
                "location_name": self.hass.config.location_name,
                "entities": sum(
                    len(self.hass.states.async_entity_ids(domain))
                    for domain in READ_DOMAINS
                ),
            }
        if kind == "entities":
            return self._entities(message.get("domains"))
        if kind == "states":
            return self._states(message.get("entity_ids"))
        if kind == "call":
            return await self._async_call(message)
        return {"ok": False, "error": "unknown_type"}

    def _entities(self, domains: Any) -> dict[str, Any]:
        if domains is None:
            wanted = list(READ_DOMAINS)
        elif isinstance(domains, list) and all(isinstance(d, str) for d in domains):
            # Only the readable domains, whatever is asked for.
            wanted = [d for d in dict.fromkeys(domains) if d in READ_DOMAINS]
        else:
            return {"ok": False, "error": "bad_request"}
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        areas = ar.async_get(self.hass)
        result = []
        for state in self.hass.states.async_all(wanted):
            area_id: str | None = None
            entry = entities.async_get(state.entity_id)
            if entry is not None:
                area_id = entry.area_id
                if area_id is None and entry.device_id:
                    device = devices.async_get(entry.device_id)
                    area_id = device.area_id if device else None
            area = areas.async_get_area(area_id) if area_id else None
            result.append(
                {
                    "entity_id": state.entity_id,
                    "state": state.state,
                    "attributes": dict(state.attributes),
                    "name": state.name,
                    "area": area.name if area else None,
                }
            )
        result.sort(key=lambda e: e["entity_id"])
        return {"ok": True, "entities": result}

    def _states(self, entity_ids: Any) -> dict[str, Any]:
        if not isinstance(entity_ids, list) or not all(
            isinstance(e, str) for e in entity_ids
        ):
            return {"ok": False, "error": "bad_request"}
        if len(entity_ids) > MAX_STATES:
            return {"ok": False, "error": "too_many"}
        states: dict[str, Any] = {}
        for entity_id in entity_ids:
            state = (
                self.hass.states.get(entity_id)
                if _domain(entity_id) in READ_DOMAINS
                else None
            )
            states[entity_id] = (
                None
                if state is None
                else {"state": state.state, "attributes": dict(state.attributes)}
            )
        return {"ok": True, "states": states}

    async def _async_call(self, message: dict[str, Any]) -> dict[str, Any]:
        domain = message.get("domain")
        service = message.get("service")
        data = message.get("data")
        if not isinstance(domain, str) or not isinstance(service, str) or not service:
            return {"ok": False, "error": "bad_request"}
        if domain not in CALL_DOMAINS:
            return {"ok": False, "error": "forbidden"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "bad_request"}
        ids = _entity_ids(data.get("entity_id"))
        if ids is None or any(k in data for k in FORBIDDEN_TARGETS):
            return {"ok": False, "error": "bad_request"}
        if any(_domain(i) != domain for i in ids):
            return {"ok": False, "error": "forbidden"}
        if not self.hass.services.has_service(domain, service):
            return {"ok": False, "error": "unknown_service"}
        try:
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT):
                await self.hass.services.async_call(
                    domain,
                    service,
                    {**data, "entity_id": ids},
                    blocking=True,
                    context=Context(),
                )
        except (HomeAssistantError, vol.Invalid, TimeoutError) as err:
            _LOGGER.warning(
                "Hotel TV: %s.%s for %s failed: %s", domain, service, ids, err
            )
            return {"ok": False, "error": "failed"}
        return {"ok": True}

    # -- TV → HA (§4) ----------------------------------------------------------

    async def _async_tv_message(
        self, tv_id: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        bridge = self.hub.bridge
        if bridge is None:
            return {"ok": False, "error": "not_ready"}
        kind = message.get("type")
        if kind == "tv.controls":
            return {
                "ok": True,
                "controls": [
                    {f: control.get(f) for f in TV_CONTROL_FIELDS}
                    for control in bridge.definitions(tv_id)
                ],
            }
        if kind == "tv.control":
            key = message.get("key")
            action = message.get("action")
            if not isinstance(key, str) or not isinstance(action, str):
                return {"ok": False, "error": "bad_request"}
            error = await bridge.async_execute(
                tv_id, key, action, message.get("params")
            )
            if error is not None:
                return {"ok": False, "error": error}
            return {"ok": True, "state": bridge.control_state(key)}
        return {"ok": False, "error": "unknown_type"}
