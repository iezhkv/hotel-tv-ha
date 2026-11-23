"""Options flow: the menu, a TV's entities, fill other TVs by pattern."""

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

from .conftest import API, ENTRY_DATA, PROPERTY, TV101, TV102, TVS, calls

T203 = "22222222-2222-4222-8222-222222222203"
T204 = "22222222-2222-4222-8222-222222222204"
T205 = "22222222-2222-4222-8222-222222222205"
T206 = "22222222-2222-4222-8222-222222222206"


def _tv(tv_id: str, name: str) -> dict[str, Any]:
    tv = copy.deepcopy(TVS[1])
    tv.update(id=tv_id, name=name, package_id=None)
    return tv


# Listed out of order on purpose: the flow sorts by name.
FLOOR_2 = [
    _tv(T204, "TV 204"),
    _tv(T203, "TV 203"),
    _tv(T205, "TV 205"),
    _tv(T206, "TV 206"),
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
    assert result["menu_options"] == ["map_tv", "fill_pattern"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


def _select_options(result: dict, field: str) -> list[dict[str, str]]:
    for key, selector in result["data_schema"].schema.items():
        if key == field:
            return list(selector.config["options"])
    raise AssertionError(field)


async def test_map_tv_link_a_thermostat(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Menu → TV → entities → labels; stored as {tv_id: {entity_id: label}}."""
    _entities(hass)
    entry = await setup_entry(tvs=FLOOR_2)
    result = await _menu(hass, entry, "map_tv")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "map_tv"
    assert [o["label"] for o in _select_options(result, "tv")] == [
        "TV 203",
        "TV 204",
        "TV 205",
        "TV 206",
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tv": T203}
    )
    assert result["step_id"] == "tv_entities"
    assert result["description_placeholders"] == {"tv": "TV 203"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"entities": [THERMOSTAT.format(203), LIGHT.format(203)]},
    )
    assert result["step_id"] == "tv_labels"
    assert "Thermostat 203" in result["description_placeholders"]["entities"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {THERMOSTAT.format(203): " Климатик "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {"controls": {T203: SOURCE}}
    (put,) = calls(aioclient_mock, "put", f"{API}/devices/{T203}/controls")
    thermostat, light = put["controls"]
    assert thermostat["label"] == "Климатик"
    assert thermostat["capabilities"]["power"] is True
    assert thermostat["capabilities"]["target_temperature"] is True
    assert thermostat["capabilities"]["step"] == 0.1
    assert light["key"] == LIGHT.format(203)

    # The TV now shows how many entities it has.
    result = await _menu(hass, entry, "map_tv")
    assert _select_options(result, "tv")[0]["label"] == "TV 203 · ✓ 2"


async def test_fill_by_pattern(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """One mapped TV is copied to the others; found and missing are shown first."""
    _entities(hass)
    entry = await setup_entry(options={"controls": {T203: SOURCE}}, tvs=FLOOR_2)
    result = await _menu(hass, entry, "fill_pattern")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fill_pattern"
    # Only TVs that have entities can be the source.
    assert _select_options(result, "source") == [
        {"value": T203, "label": "TV 203 · ✓ 2"}
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": T203}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fill_confirm"
    assert result["description_placeholders"] == {
        "tvs": "2",
        "result": "\n".join(
            [
                "**TV 204**",
                f"- ✓ `{THERMOSTAT.format(204)}`",
                f"- ✓ `{LIGHT.format(204)}`",
                "**TV 205**",
                f"- ✓ `{THERMOSTAT.format(205)}`",
                f"- ✗ `{LIGHT.format(205)}`",
                "**✗ TV 206**",
            ]
        ),
    }
    # Nothing is saved or sent before the confirmation.
    assert entry.options == {"controls": {T203: SOURCE}}
    assert calls(aioclient_mock, "put", f"{API}/devices/{T204}/controls") == []

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {
        "controls": {
            T203: SOURCE,
            T204: {THERMOSTAT.format(204): "Климатик", LIGHT.format(204): ""},
            T205: {THERMOSTAT.format(205): "Климатик"},
        }
    }
    (put204,) = calls(aioclient_mock, "put", f"{API}/devices/{T204}/controls")
    assert [(c["key"], c["label"]) for c in put204["controls"]] == [
        (THERMOSTAT.format(204), "Климатик"),
        (LIGHT.format(204), LIGHT.format(204)),
    ]
    (put205,) = calls(aioclient_mock, "put", f"{API}/devices/{T205}/controls")
    assert [c["key"] for c in put205["controls"]] == [THERMOSTAT.format(205)]
    # The source and the TV with nothing found are not sent again.
    assert len(calls(aioclient_mock, "put", f"{API}/devices/{T203}/controls")) == 1
    assert calls(aioclient_mock, "put", f"{API}/devices/{T206}/controls") == []


async def test_fill_by_pattern_overwrite(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """TVs that already have entities are kept unless overwrite is ticked."""
    _entities(hass)
    hass.states.async_set("switch.room_204_fan", "off", {})
    own = {"switch.room_204_fan": "Вентилатор"}
    entry = await setup_entry(
        options={"controls": {T203: SOURCE, T204: own}}, tvs=FLOOR_2
    )

    result = await _menu(hass, entry, "fill_pattern")
    assert [o["value"] for o in _select_options(result, "source")] == [T203, T204]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": T203, "overwrite": False}
    )
    assert result["step_id"] == "fill_confirm"
    assert "**TV 204**" not in result["description_placeholders"]["result"]
    assert result["description_placeholders"]["tvs"] == "1"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options["controls"][T204] == own
    assert entry.options["controls"][T205] == {THERMOSTAT.format(205): "Климатик"}

    result = await _menu(hass, entry, "fill_pattern")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": T203, "overwrite": True}
    )
    assert result["description_placeholders"]["tvs"] == "2"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["controls"][T204] == {
        THERMOSTAT.format(204): "Климатик",
        LIGHT.format(204): "",
    }
    assert entry.options["controls"][T203] == SOURCE
    last = calls(aioclient_mock, "put", f"{API}/devices/{T204}/controls")[-1]
    assert [c["key"] for c in last["controls"]] == [
        THERMOSTAT.format(204),
        LIGHT.format(204),
    ]


async def test_fill_by_pattern_nothing_found(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """No counterpart anywhere: an error on the form, nothing saved."""
    hass.states.async_set("switch.lobby", "off", {})
    options = {"controls": {TV101: {"switch.lobby": "Лоби"}}}
    entry = await setup_entry(options=options)
    result = await _menu(hass, entry, "fill_pattern")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": TV101, "overwrite": True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fill_pattern"
    assert result["errors"] == {"base": "nothing_found"}
    assert entry.options == options


async def test_fill_by_pattern_needs_a_mapped_tv(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """Without any TV set up there is nothing to copy."""
    entry = await setup_entry()
    result = await _menu(hass, entry, "fill_pattern")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_mapped_tvs"


async def test_options_not_loaded(hass: HomeAssistant) -> None:
    """The TVs come from the running integration."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
    assert TV102 not in entry.options.get("controls", {})


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
        "map_tv",
        "tv_entities",
        "tv_labels",
        "fill_pattern",
        "fill_confirm",
    }
    assert set(en["options"]["step"]["init"]["menu_options"]) == {
        "map_tv",
        "fill_pattern",
    }
