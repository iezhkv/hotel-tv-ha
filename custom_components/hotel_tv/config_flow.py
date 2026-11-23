"""Config flow (server address + pairing code), reauth, reconfigure, TV controls."""

from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_TOKEN, CONF_URL, __version__ as HA_VERSION
from homeassistant.core import callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    HotelTvConnectionError,
    HotelTvError,
    async_pair,
    normalize_code,
    normalize_url,
)
from .const import (
    CONF_CODE,
    CONF_CONNECTION_ID,
    CONF_PROPERTY_ID,
    CONF_PROPERTY_NAME,
    CONF_SECRET,
    CONF_WEBHOOK_ID,
    CONTROL_DOMAINS,
    DOMAIN,
    OPT_CONTROLS,
)
from .mapping import Proposal, Target, fill_by_pattern
from .wire import local_webhook_urls, pairing_handler

_LOGGER = logging.getLogger(__name__)

URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
CODE_SELECTOR = TextSelector()

# The server's error slugs of POST /ha/pair → the form's errors.
PAIR_ERRORS = {
    "invalid_code": "invalid_code",
    "expired_code": "expired_code",
    "rate_limited": "rate_limited",
    "unreachable": "unreachable",
    "bad_request": "invalid_code",
    "not_found": "invalid_code",
    "not_hotel_tv": "not_hotel_tv",
}


def _natural(text: str) -> tuple[Any, ...]:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text))


def _lan_host(host: str | None) -> bool:
    """A private-range address or a LAN-only name (http allowed there only)."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        name = host.lower().rstrip(".")
        return "." not in name or name.endswith((".local", ".lan", ".home.arpa"))
    return ip.is_private or ip.is_loopback or ip.is_link_local


class HotelTvConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair Home Assistant with one Hotel TV property."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._webhook_id: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Which entities guests may control from each TV."""
        return HotelTvOptionsFlow()

    async def _async_pair(
        self, url: str, code: str, errors: dict[str, str]
    ) -> dict[str, Any] | None:
        """POST /api/v1/ha/pair; fill `errors` and return None on failure.

        While the request runs, a fresh webhook id answers the server's probe: a
        `ping` signed with a one-time probe token (docs/HA-PAIRING.md §1).
        """
        try:
            url = normalize_url(url)
        except ValueError:
            errors[CONF_URL] = "invalid_url"
            return None
        parts = urlsplit(url)
        if parts.scheme != "https" and not _lan_host(parts.hostname):
            errors[CONF_URL] = "insecure_url"
            return None
        if not code.strip():
            errors[CONF_CODE] = "invalid_code"
            return None
        if self._webhook_id is None:
            self._webhook_id = webhook.async_generate_id()
        urls = local_webhook_urls(self.hass, self._webhook_id)
        if not urls:
            errors["base"] = "no_ha_url"
            return None
        probe_token = secrets.token_hex(32)
        webhook.async_register(
            self.hass,
            DOMAIN,
            "Hotel TV pairing",
            self._webhook_id,
            pairing_handler(probe_token),
            local_only=False,
            allowed_methods=["POST"],
        )
        try:
            answer = await async_pair(
                async_get_clientsession(self.hass),
                url,
                normalize_code(code),
                {
                    "instance_id": await instance_id.async_get(self.hass),
                    "version": HA_VERSION,
                    "location_name": self.hass.config.location_name,
                    "webhook_urls": urls,
                    "probe_token": probe_token,
                },
            )
        except HotelTvConnectionError:
            errors["base"] = "cannot_connect"
            return None
        except HotelTvError as err:
            errors["base"] = PAIR_ERRORS.get(err.code, "unknown")
            return None
        except Exception:
            _LOGGER.exception("Unexpected error while pairing with Hotel TV")
            errors["base"] = "unknown"
            return None
        finally:
            webhook.async_unregister(self.hass, self._webhook_id)

        prop = answer["property"]
        data = {
            CONF_URL: url,
            CONF_TOKEN: str(answer["token"]),
            CONF_SECRET: str(answer["secret"]),
            CONF_CONNECTION_ID: str(answer["connection_id"]),
            CONF_WEBHOOK_ID: self._webhook_id,
            CONF_PROPERTY_ID: str(prop["id"]),
            CONF_PROPERTY_NAME: str(prop.get("name") or prop["id"]),
        }
        # The next pairing (another attempt in this flow) uses a new webhook id.
        self._webhook_id = None
        return data

    def _schema(self, defaults: Mapping[str, Any], *, url: bool = True) -> vol.Schema:
        fields: dict[Any, Any] = {}
        if url:
            fields[vol.Required(CONF_URL)] = URL_SELECTOR
        fields[vol.Required(CONF_CODE)] = CODE_SELECTOR
        return self.add_suggested_values_to_schema(vol.Schema(fields), defaults)

    async def _async_other_property(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Reauth / reconfigure answered with a code of ANOTHER property.

        The server has already replaced that property's connection with this one:
        its entry (when there is one) takes it over; otherwise it stays unused.
        """
        other = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, data[CONF_PROPERTY_ID]
        )
        if other is None:
            return self.async_abort(reason="wrong_property")
        self.hass.config_entries.async_update_entry(
            other, title=data[CONF_PROPERTY_NAME], data={**other.data, **data}
        )
        self.hass.config_entries.async_schedule_reload(other.entry_id)
        return self.async_abort(
            reason="other_property_updated",
            description_placeholders={"property": data[CONF_PROPERTY_NAME]},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Server address + the pairing code from Настройки → Home Assistant."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_pair(
                user_input[CONF_URL], user_input[CONF_CODE], errors
            )
            if data is not None:
                await self.async_set_unique_id(data[CONF_PROPERTY_ID])
                # Paired again: the existing entry takes the new connection.
                self._abort_if_unique_id_configured(updates=data)
                return self.async_create_entry(
                    title=data[CONF_PROPERTY_NAME], data=data
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self._schema({CONF_URL: (user_input or {}).get(CONF_URL)}),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The server refused the token (or the entry was never paired)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new pairing code."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_pair(
                entry.data[CONF_URL], user_input[CONF_CODE], errors
            )
            if data is not None:
                if data[CONF_PROPERTY_ID] != entry.unique_id:
                    return await self._async_other_property(data)
                return self.async_update_reload_and_abort(
                    entry, title=data[CONF_PROPERTY_NAME], data_updates=data
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._schema({}, url=False),
            errors=errors,
            description_placeholders={
                "property": entry.title,
                "url": str(entry.data.get(CONF_URL) or ""),
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A new server address of the same property (pairs again with a code)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_pair(
                user_input[CONF_URL], user_input[CONF_CODE], errors
            )
            if data is not None:
                if data[CONF_PROPERTY_ID] != entry.unique_id:
                    return await self._async_other_property(data)
                return self.async_update_reload_and_abort(
                    entry, title=data[CONF_PROPERTY_NAME], data_updates=data
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema(
                {CONF_URL: (user_input or entry.data).get(CONF_URL)}
            ),
            errors=errors,
        )


class HotelTvOptionsFlow(OptionsFlow):
    """Per TV: which HA entities the guest may control from it."""

    def __init__(self) -> None:
        """Initialize."""
        self._tv_id: str | None = None
        self._entities: list[str] = []
        self._proposal: dict[str, Proposal] = {}

    @property
    def _tvs(self) -> dict[str, dict[str, Any]]:
        hub = getattr(self.config_entry, "runtime_data", None)
        return dict(hub.tvs) if hub is not None else {}

    @property
    def _controls(self) -> dict[str, dict[str, str]]:
        return {
            tv: dict(entities)
            for tv, entities in (
                self.config_entry.options.get(OPT_CONTROLS) or {}
            ).items()
        }

    def _tv_name(self, tv_id: str) -> str:
        return str(self._tvs.get(tv_id, {}).get("name") or tv_id)

    def _save(self, tv_id: str, entities: dict[str, str]) -> ConfigFlowResult:
        controls = self._controls
        if entities:
            controls[tv_id] = entities
        else:
            controls.pop(tv_id, None)
        return self.async_create_entry(
            data={**self.config_entry.options, OPT_CONTROLS: controls}
        )

    def _tv_options(self, tv_ids: list[str]) -> list[SelectOptionDict]:
        controls = self._controls
        options = []
        for tv_id in sorted(tv_ids, key=lambda t: _natural(self._tv_name(t))):
            label = self._tv_name(tv_id)
            if controls.get(tv_id):
                label += f" · ✓ {len(controls[tv_id])}"
            options.append(SelectOptionDict(value=tv_id, label=label))
        return options

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu: map one TV, or copy a mapped TV to the others."""
        if not self._tvs:
            return self.async_abort(reason="not_loaded")
        return self.async_show_menu(
            step_id="init", menu_options=["map_tv", "fill_pattern"]
        )

    async def async_step_map_tv(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a TV."""
        if user_input is not None:
            self._tv_id = user_input["tv"]
            return await self.async_step_tv_entities()
        schema = vol.Schema(
            {
                vol.Required("tv"): SelectSelector(
                    SelectSelectorConfig(
                        options=self._tv_options(list(self._tvs)),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="map_tv", data_schema=schema)

    async def async_step_fill_pattern(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Copy one TV's entities to the other TVs by the number in the TV names."""
        tvs = self._tvs
        controls = self._controls
        sources = [tv_id for tv_id in controls if tv_id in tvs]
        if not sources:
            return self.async_abort(reason="no_mapped_tvs")
        errors: dict[str, str] = {}
        if user_input is not None:
            source = user_input["source"]
            self._proposal = fill_by_pattern(
                self._tv_name(source),
                controls.get(source, {}),
                {
                    tv_id: Target(self._tv_name(tv_id), controls.get(tv_id, {}))
                    for tv_id in tvs
                    if tv_id != source
                },
                lambda entity_id: self.hass.states.get(entity_id) is not None,
                overwrite=bool(user_input.get("overwrite")),
            )
            if any(p.entities for p in self._proposal.values()):
                return await self.async_step_fill_confirm()
            errors["base"] = "nothing_found"
        schema = vol.Schema(
            {
                vol.Required("source"): SelectSelector(
                    SelectSelectorConfig(
                        options=self._tv_options(sources),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("overwrite", default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="fill_pattern",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_fill_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what was found and what was not, per TV; save on confirm."""
        found = {
            tv_id: proposal
            for tv_id, proposal in self._proposal.items()
            if proposal.entities
        }
        if user_input is not None:
            controls = self._controls
            for tv_id, proposal in found.items():
                controls[tv_id] = dict(proposal.entities)
            return self.async_create_entry(
                data={**self.config_entry.options, OPT_CONTROLS: controls}
            )
        by_name = sorted(self._proposal, key=lambda t: _natural(self._tv_name(t)))
        lines: list[str] = []
        for tv_id in by_name:
            if tv_id not in found:
                continue
            lines.append(f"**{self._tv_name(tv_id)}**")
            lines.extend(f"- ✓ `{entity_id}`" for entity_id in found[tv_id].entities)
            lines.extend(f"- ✗ `{entity_id}`" for entity_id in found[tv_id].missing)
        nothing = [self._tv_name(t) for t in by_name if t not in found]
        if nothing:
            lines.append(f"**✗ {', '.join(nothing)}**")
        return self.async_show_form(
            step_id="fill_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "tvs": str(len(found)),
                "result": "\n".join(lines),
            },
        )

    async def async_step_tv_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the TV's entities (none = the guest controls nothing)."""
        assert self._tv_id is not None
        current = self._controls.get(self._tv_id, {})
        if user_input is not None:
            self._entities = [
                e
                for e in dict.fromkeys(user_input.get("entities") or [])
                if e.partition(".")[0] in CONTROL_DOMAINS
            ]
            if not self._entities:
                return self._save(self._tv_id, {})
            return await self.async_step_tv_labels()
        schema = vol.Schema(
            {
                vol.Optional(
                    "entities", description={"suggested_value": list(current)}
                ): EntitySelector(
                    EntitySelectorConfig(domain=list(CONTROL_DOMAINS), multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="tv_entities",
            data_schema=schema,
            description_placeholders={"tv": self._tv_name(self._tv_id)},
        )

    async def async_step_tv_labels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optional label shown on the TV for each entity (empty = its HA name)."""
        assert self._tv_id is not None
        if user_input is not None:
            entities = {
                entity_id: str(user_input.get(entity_id) or "").strip()
                for entity_id in self._entities
            }
            return self._save(self._tv_id, entities)
        current = self._controls.get(self._tv_id, {})
        fields: dict[Any, Any] = {}
        for entity_id in self._entities:
            fields[
                vol.Optional(
                    entity_id,
                    description={"suggested_value": current.get(entity_id) or None},
                )
            ] = TextSelector()
        names = "\n".join(
            f"- `{entity_id}`: {self._friendly(entity_id)}"
            for entity_id in self._entities
        )
        return self.async_show_form(
            step_id="tv_labels",
            data_schema=vol.Schema(fields),
            description_placeholders={
                "tv": self._tv_name(self._tv_id),
                "entities": names,
            },
        )

    def _friendly(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        if state is None:
            return entity_id
        return str(state.attributes.get("friendly_name") or entity_id)
