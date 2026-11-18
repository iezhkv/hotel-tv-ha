"""Config flow (server URL + API token), reauth, reconfigure and the room controls."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import callback
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
    HotelTvAuthError,
    HotelTvClient,
    HotelTvConnectionError,
    HotelTvError,
    normalize_url,
)
from .const import (
    CONF_PROPERTY_ID,
    CONF_PROPERTY_NAME,
    CONTROL_DOMAINS,
    DOMAIN,
    OPT_CONTROLS,
)
from .mapping import Proposal, Target, fill_by_pattern

_LOGGER = logging.getLogger(__name__)

URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _natural(text: str) -> tuple[Any, ...]:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text))


class HotelTvConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect Home Assistant to one Hotel TV property."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Which entities guests may control from each room's TV."""
        return HotelTvOptionsFlow()

    async def _async_validate(
        self, url: str, token: str, errors: dict[str, str]
    ) -> dict[str, Any] | None:
        """GET /info; fill `errors` and return None on failure."""
        try:
            url = normalize_url(url)
        except ValueError:
            errors[CONF_URL] = "invalid_url"
            return None
        client = HotelTvClient(async_get_clientsession(self.hass), url, token.strip())
        try:
            info = await client.async_get_info()
        except HotelTvAuthError:
            errors[CONF_TOKEN] = "invalid_auth"
            return None
        except HotelTvConnectionError:
            errors["base"] = "cannot_connect"
            return None
        except HotelTvError:
            errors["base"] = "not_hotel_tv"
            return None
        except Exception:
            _LOGGER.exception("Unexpected error while connecting to Hotel TV")
            errors["base"] = "unknown"
            return None
        prop = info["property"]
        return {
            CONF_URL: url,
            CONF_TOKEN: token.strip(),
            CONF_PROPERTY_ID: str(prop["id"]),
            CONF_PROPERTY_NAME: str(prop.get("name") or prop["id"]),
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Server address and API token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_validate(
                user_input[CONF_URL], user_input[CONF_TOKEN], errors
            )
            if data is not None:
                await self.async_set_unique_id(data[CONF_PROPERTY_ID])
                self._abort_if_unique_id_configured(
                    updates={CONF_URL: data[CONF_URL], CONF_TOKEN: data[CONF_TOKEN]}
                )
                return self.async_create_entry(
                    title=data[CONF_PROPERTY_NAME], data=data
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL): URL_SELECTOR,
                vol.Required(CONF_TOKEN): TOKEN_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_URL: (user_input or {}).get(CONF_URL)}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The server refused the token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_validate(
                entry.data[CONF_URL], user_input[CONF_TOKEN], errors
            )
            if data is not None:
                await self.async_set_unique_id(data[CONF_PROPERTY_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_property")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: data[CONF_TOKEN]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR}),
            errors=errors,
            description_placeholders={"property": entry.title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server address (and token) of the same property."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_validate(
                user_input[CONF_URL],
                user_input.get(CONF_TOKEN) or entry.data[CONF_TOKEN],
                errors,
            )
            if data is not None:
                await self.async_set_unique_id(data[CONF_PROPERTY_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_property")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_URL: data[CONF_URL],
                        CONF_TOKEN: data[CONF_TOKEN],
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL): URL_SELECTOR,
                vol.Optional(CONF_TOKEN): TOKEN_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_URL: (user_input or entry.data).get(CONF_URL)}
            ),
            errors=errors,
        )


class HotelTvOptionsFlow(OptionsFlow):
    """Per room: which HA entities the guest may control from the TV."""

    def __init__(self) -> None:
        """Initialize."""
        self._room_id: str | None = None
        self._entities: list[str] = []
        self._proposal: dict[str, Proposal] = {}

    @property
    def _rooms(self) -> dict[str, dict[str, Any]]:
        hub = getattr(self.config_entry, "runtime_data", None)
        return dict(hub.rooms) if hub is not None else {}

    @property
    def _controls(self) -> dict[str, dict[str, str]]:
        return {
            room: dict(entities)
            for room, entities in (
                self.config_entry.options.get(OPT_CONTROLS) or {}
            ).items()
        }

    def _room_name(self, room_id: str) -> str:
        return str(self._rooms.get(room_id, {}).get("name") or room_id)

    def _save(self, room_id: str, entities: dict[str, str]) -> ConfigFlowResult:
        controls = self._controls
        if entities:
            controls[room_id] = entities
        else:
            controls.pop(room_id, None)
        return self.async_create_entry(
            data={**self.config_entry.options, OPT_CONTROLS: controls}
        )

    def _room_options(self, room_ids: list[str]) -> list[SelectOptionDict]:
        rooms = self._rooms
        controls = self._controls
        options = []
        for room_id in sorted(room_ids, key=lambda r: _natural(self._room_name(r))):
            label = self._room_name(room_id)
            if rooms.get(room_id, {}).get("floor"):
                label += f" · {rooms[room_id]['floor']}"
            if controls.get(room_id):
                label += f" · ✓ {len(controls[room_id])}"
            options.append(SelectOptionDict(value=room_id, label=label))
        return options

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu: map one room, or copy a mapped room to the others."""
        if not self._rooms:
            return self.async_abort(reason="not_loaded")
        return self.async_show_menu(
            step_id="init", menu_options=["map_room", "fill_pattern"]
        )

    async def async_step_map_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a room."""
        if user_input is not None:
            self._room_id = user_input["room"]
            return await self.async_step_room_entities()
        schema = vol.Schema(
            {
                vol.Required("room"): SelectSelector(
                    SelectSelectorConfig(
                        options=self._room_options(list(self._rooms)),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="map_room", data_schema=schema)

    async def async_step_fill_pattern(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Copy one room's entities to the other rooms by the room name in the ids."""
        rooms = self._rooms
        controls = self._controls
        sources = [room_id for room_id in controls if room_id in rooms]
        if not sources:
            return self.async_abort(reason="no_mapped_rooms")
        errors: dict[str, str] = {}
        if user_input is not None:
            source = user_input["source"]
            self._proposal = fill_by_pattern(
                self._room_name(source),
                controls.get(source, {}),
                {
                    room_id: Target(self._room_name(room_id), controls.get(room_id, {}))
                    for room_id in rooms
                    if room_id != source
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
                        options=self._room_options(sources),
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
        """Show what was found and what was not, per room; save on confirm."""
        found = {
            room_id: proposal
            for room_id, proposal in self._proposal.items()
            if proposal.entities
        }
        if user_input is not None:
            controls = self._controls
            for room_id, proposal in found.items():
                controls[room_id] = dict(proposal.entities)
            return self.async_create_entry(
                data={**self.config_entry.options, OPT_CONTROLS: controls}
            )
        by_name = sorted(self._proposal, key=lambda r: _natural(self._room_name(r)))
        lines: list[str] = []
        for room_id in by_name:
            if room_id not in found:
                continue
            lines.append(f"**{self._room_name(room_id)}**")
            lines.extend(f"- ✓ `{entity_id}`" for entity_id in found[room_id].entities)
            lines.extend(f"- ✗ `{entity_id}`" for entity_id in found[room_id].missing)
        nothing = [self._room_name(r) for r in by_name if r not in found]
        if nothing:
            lines.append(f"**✗ {', '.join(nothing)}**")
        return self.async_show_form(
            step_id="fill_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "rooms": str(len(found)),
                "result": "\n".join(lines),
            },
        )

    async def async_step_room_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the room's entities (none = the guest controls nothing)."""
        assert self._room_id is not None
        current = self._controls.get(self._room_id, {})
        if user_input is not None:
            self._entities = [
                e
                for e in dict.fromkeys(user_input.get("entities") or [])
                if e.partition(".")[0] in CONTROL_DOMAINS
            ]
            if not self._entities:
                return self._save(self._room_id, {})
            return await self.async_step_room_labels()
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
            step_id="room_entities",
            data_schema=schema,
            description_placeholders={"room": self._room_name(self._room_id)},
        )

    async def async_step_room_labels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optional label shown on the TV for each entity (empty = its HA name)."""
        assert self._room_id is not None
        if user_input is not None:
            entities = {
                entity_id: str(user_input.get(entity_id) or "").strip()
                for entity_id in self._entities
            }
            return self._save(self._room_id, entities)
        current = self._controls.get(self._room_id, {})
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
            step_id="room_labels",
            data_schema=vol.Schema(fields),
            description_placeholders={
                "room": self._room_name(self._room_id),
                "entities": names,
            },
        )

    def _friendly(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        if state is None:
            return entity_id
        return str(state.attributes.get("friendly_name") or entity_id)
