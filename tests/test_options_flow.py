"""Options flow: the menu, a room's entities, fill other rooms by pattern."""

from __future__ import annotations

from collections.abc import Callable
import copy
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.hotel_tv.const import DOMAIN

from .conftest import API, ENTRY_DATA, PROPERTY, R101, R102, ROOMS, calls

R203 = "11111111-1111-4111-8111-111111111203"
R204 = "11111111-1111-4111-8111-111111111204"
R205 = "11111111-1111-4111-8111-111111111205"
R206 = "11111111-1111-4111-8111-111111111206"


def _room(room_id: str, name: str) -> dict[str, Any]:
    room = copy.deepcopy(ROOMS[1])
    room.update(id=room_id, name=name, floor="2", package_id=None)
    return room


FLOOR_2 = [
    _room(R203, "203"),
    _room(R204, "204"),
    _room(R205, "205"),
    _room(R206, "206"),
]
THERMOSTAT = "climate.room_{}_thermostat_climate_controller_2"
LIGHT = "light.room_{}_main"
SOURCE = {THERMOSTAT.format(203): "Климатик", LIGHT.format(203): ""}
THERMOSTAT_ATTRS = {
    "hvac_modes": ["off", "cool", "heat"],
    "min_temp": 15,
    "max_temp": 30,
    "target_temp_step": 0.1,
    "current_temperature": 22.5,
    "temperature": 22,
    "hvac_action": "idle",
    "supported_features": 387,
}


def _entities(hass: HomeAssistant) -> None:
    """203 and 204 have both entities, 205 only the thermostat, 206 nothing."""
    for number in (203, 204, 205):
        hass.states.async_set(
            THERMOSTAT.format(number),
            "heat",
            {**THERMOSTAT_ATTRS, "friendly_name": f"Thermostat {number}"},
        )
    for number in (203, 204):
        hass.states.async_set(LIGHT.format(number), "off", {})


async def _menu(hass: HomeAssistant, entry: MockConfigEntry, step: str) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["map_room", "fill_pattern"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


def _select_options(result: dict, field: str) -> list[dict[str, str]]:
    for key, selector in result["data_schema"].schema.items():
        if key == field:
            return list(selector.config["options"])
    raise AssertionError(field)


async def test_map_room_link_a_thermostat(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Menu → room → entities → labels; the stored format is unchanged."""
    _entities(hass)
    entry = await setup_entry(rooms=FLOOR_2)
    result = await _menu(hass, entry, "map_room")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "map_room"
    assert [o["label"] for o in _select_options(result, "room")] == [
        "203 · 2",
        "204 · 2",
        "205 · 2",
        "206 · 2",
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"room": R203}
    )
    assert result["step_id"] == "room_entities"
    assert result["description_placeholders"] == {"room": "203"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"entities": [THERMOSTAT.format(203), LIGHT.format(203)]},
    )
    assert result["step_id"] == "room_labels"
    assert "Thermostat 203" in result["description_placeholders"]["entities"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {THERMOSTAT.format(203): " Климатик "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {"controls": {R203: SOURCE}}
    (put,) = calls(aioclient_mock, "put", f"{API}/rooms/{R203}/controls")
    thermostat, light = put["controls"]
    assert thermostat["label"] == "Климатик"
    assert thermostat["capabilities"]["power"] is True
    assert thermostat["capabilities"]["target_temperature"] is True
    assert thermostat["capabilities"]["step"] == 0.1
    assert light["key"] == LIGHT.format(203)

    # The room now shows how many entities it has.
    result = await _menu(hass, entry, "map_room")
    assert _select_options(result, "room")[0]["label"] == "203 · 2 · ✓ 2"


async def test_fill_by_pattern(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """One mapped room is copied to the others; found and missing are shown first."""
    _entities(hass)
    entry = await setup_entry(options={"controls": {R203: SOURCE}}, rooms=FLOOR_2)
    result = await _menu(hass, entry, "fill_pattern")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fill_pattern"
    # Only rooms that have entities can be the source.
    assert _select_options(result, "source") == [
        {"value": R203, "label": "203 · 2 · ✓ 2"}
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": R203}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fill_confirm"
    assert result["description_placeholders"] == {
        "rooms": "2",
        "result": "\n".join(
            [
                "**204**",
                f"- ✓ `{THERMOSTAT.format(204)}`",
                f"- ✓ `{LIGHT.format(204)}`",
                "**205**",
                f"- ✓ `{THERMOSTAT.format(205)}`",
                f"- ✗ `{LIGHT.format(205)}`",
                "**✗ 206**",
            ]
        ),
    }
    # Nothing is saved or sent before the confirmation.
    assert entry.options == {"controls": {R203: SOURCE}}
    assert calls(aioclient_mock, "put", f"{API}/rooms/{R204}/controls") == []

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {
        "controls": {
            R203: SOURCE,
            R204: {THERMOSTAT.format(204): "Климатик", LIGHT.format(204): ""},
            R205: {THERMOSTAT.format(205): "Климатик"},
        }
    }
    (put204,) = calls(aioclient_mock, "put", f"{API}/rooms/{R204}/controls")
    assert [(c["key"], c["label"]) for c in put204["controls"]] == [
        (THERMOSTAT.format(204), "Климатик"),
        (LIGHT.format(204), LIGHT.format(204)),
    ]
    (put205,) = calls(aioclient_mock, "put", f"{API}/rooms/{R205}/controls")
    assert [c["key"] for c in put205["controls"]] == [THERMOSTAT.format(205)]
    # The source and the room with nothing found are not sent again.
    assert len(calls(aioclient_mock, "put", f"{API}/rooms/{R203}/controls")) == 1
    assert calls(aioclient_mock, "put", f"{API}/rooms/{R206}/controls") == []


async def test_fill_by_pattern_overwrite(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Rooms that already have entities are kept unless overwrite is ticked."""
    _entities(hass)
    hass.states.async_set("switch.room_204_fan", "off", {})
    own = {"switch.room_204_fan": "Вентилатор"}
    entry = await setup_entry(
        options={"controls": {R203: SOURCE, R204: own}}, rooms=FLOOR_2
    )

    result = await _menu(hass, entry, "fill_pattern")
    assert [o["value"] for o in _select_options(result, "source")] == [R203, R204]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": R203, "overwrite": False}
    )
    assert result["step_id"] == "fill_confirm"
    assert "**204**" not in result["description_placeholders"]["result"]
    assert result["description_placeholders"]["rooms"] == "1"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options["controls"][R204] == own
    assert entry.options["controls"][R205] == {THERMOSTAT.format(205): "Климатик"}

    result = await _menu(hass, entry, "fill_pattern")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": R203, "overwrite": True}
    )
    assert result["description_placeholders"]["rooms"] == "2"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["controls"][R204] == {
        THERMOSTAT.format(204): "Климатик",
        LIGHT.format(204): "",
    }
    assert entry.options["controls"][R203] == SOURCE
    last = calls(aioclient_mock, "put", f"{API}/rooms/{R204}/controls")[-1]
    assert [c["key"] for c in last["controls"]] == [
        THERMOSTAT.format(204),
        LIGHT.format(204),
    ]


async def test_fill_by_pattern_nothing_found(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """No counterpart anywhere: an error on the form, nothing saved."""
    hass.states.async_set("switch.lobby", "off", {})
    options = {"controls": {R101: {"switch.lobby": "Лоби"}}}
    entry = await setup_entry(options=options)
    result = await _menu(hass, entry, "fill_pattern")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": R101, "overwrite": True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fill_pattern"
    assert result["errors"] == {"base": "nothing_found"}
    assert entry.options == options


async def test_fill_by_pattern_needs_a_mapped_room(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """Without any room set up there is nothing to copy."""
    entry = await setup_entry()
    result = await _menu(hass, entry, "fill_pattern")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_mapped_rooms"


async def test_options_not_loaded(hass: HomeAssistant) -> None:
    """The rooms come from the running integration."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
    assert R102 not in entry.options.get("controls", {})


def test_translations_in_sync() -> None:
    """en and bg carry the same keys, and every options step is described."""
    import json
    from pathlib import Path

    base = Path(__file__).parent.parent / "custom_components/hotel_tv/translations"

    def keys(node: Any, prefix: str = "") -> set[str]:
        if not isinstance(node, dict):
            return {prefix}
        return {k for name, v in node.items() for k in keys(v, f"{prefix}.{name}")}

    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    bg = json.loads((base / "bg.json").read_text(encoding="utf-8"))
    assert keys(en) == keys(bg)
    assert set(en["options"]["step"]) == {
        "init",
        "map_room",
        "room_entities",
        "room_labels",
        "fill_pattern",
        "fill_confirm",
    }
    assert set(en["options"]["step"]["init"]["menu_options"]) == {
        "map_room",
        "fill_pattern",
    }
