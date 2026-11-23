"""TV controls bridge: definitions to the server, state sync, guest requests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from .conftest import API, TV101, TV102, calls

OPTIONS = {
    "controls": {
        TV101: {"climate.r101": "Климатик", "light.r101": "", "scene.r101": ""},
        TV102: {"switch.r102": ""},
    }
}
CLIMATE = {
    "friendly_name": "AC 101",
    "hvac_modes": ["off", "heat", "cool"],
    "fan_modes": ["auto", "low"],
    "min_temp": 16,
    "max_temp": 30,
    "target_temp_step": 0.5,
    "current_temperature": 21.5,
    "temperature": 22,
    "fan_mode": "auto",
    "supported_features": 1 | 8,  # TARGET_TEMPERATURE | FAN_MODE
}
LIGHT = {
    "friendly_name": "Ceiling 101",
    "supported_color_modes": ["brightness"],
    "brightness": 255,
}


@pytest.fixture
def entities(hass: HomeAssistant) -> None:
    """The HA entities of the rooms (and one that is never exposed)."""
    hass.states.async_set("climate.r101", "heat", CLIMATE)
    hass.states.async_set("light.r101", "on", LIGHT)
    hass.states.async_set("scene.r101", "unknown", {"friendly_name": "Night"})
    hass.states.async_set("switch.r102", "off", {"friendly_name": "Fan 102"})
    hass.states.async_set("switch.lobby", "off", {"friendly_name": "Lobby"})


@pytest.fixture
def server(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Accept PATCHes and acks."""
    for key in ("climate.r101", "light.r101", "scene.r101"):
        aioclient_mock.patch(f"{API}/devices/{TV101}/controls/{key}", json={})
    aioclient_mock.patch(f"{API}/devices/{TV102}/controls/switch.r102", json={})
    aioclient_mock.patch(f"{API}/devices/{TV101}/controls/switch.r102", json={})
    for request_id in range(1, 10):
        aioclient_mock.post(
            f"{API}/control-requests/{request_id}/ack", json={"ok": True}
        )
    return aioclient_mock


def _request(
    request_id: int,
    key: str,
    action: str,
    params: dict[str, Any] | None = None,
    tv_id: str = TV101,
    age: float = 0,
) -> dict[str, Any]:
    return {
        "id": 100 + request_id,
        "type": "control.requested",
        "device_id": tv_id,
        "payload": {
            "request_id": request_id,
            "control_id": "c-1",
            "key": key,
            "action": action,
            "params": params or {},
        },
        "created_at": (dt_util.utcnow() - timedelta(seconds=age)).isoformat(),
    }


def _acks(aioclient_mock: AiohttpClientMocker) -> list[tuple[str, str]]:
    out = []
    for method, url, data, _h in aioclient_mock.mock_calls:
        if method.lower() == "post" and "/control-requests/" in str(url):
            import json

            out.append((str(url).split("/")[-2], json.loads(data)["status"]))
    return out


async def _flush(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()


async def test_put_definitions_on_start(
    hass: HomeAssistant,
    entities: None,
    server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Every TV with exposed entities gets its full control list, in the chosen order."""
    await setup_entry(options=OPTIONS)
    (put101,) = calls(server, "put", f"{API}/devices/{TV101}/controls")
    assert put101 == {
        "controls": [
            {
                "key": "climate.r101",
                "kind": "climate",
                "label": "Климатик",
                "icon": None,
                "position": 0,
                "state": {
                    "hvac_mode": "heat",
                    "hvac_action": None,
                    "current_temperature": 21.5,
                    "target_temperature": 22.0,
                    "current_humidity": None,
                    "fan_mode": "auto",
                    "swing_mode": None,
                    "preset_mode": None,
                },
                "capabilities": {
                    "power": True,
                    "target_temperature": True,
                    "current_temperature": True,
                    "current_humidity": False,
                    "min_temp": 16.0,
                    "max_temp": 30.0,
                    "step": 0.5,
                    "unit": "°C",
                    "hvac_modes": ["off", "heat", "cool"],
                    "fan_modes": ["auto", "low"],
                    "swing_modes": [],
                    "preset_modes": [],
                },
                "available": True,
            },
            {
                "key": "light.r101",
                "kind": "light",
                "label": "Ceiling 101",
                "icon": None,
                "position": 1,
                "state": {"on": True, "brightness": 100},
                "capabilities": {"dimmable": True},
                "available": True,
            },
            {
                "key": "scene.r101",
                "kind": "scene",
                "label": "Night",
                "icon": None,
                "position": 2,
                "state": {},
                "capabilities": {},
                "available": True,
            },
        ]
    }
    (put102,) = calls(server, "put", f"{API}/devices/{TV102}/controls")
    assert [c["key"] for c in put102["controls"]] == ["switch.r102"]


async def test_state_changes_are_patched(
    hass: HomeAssistant,
    entities: None,
    server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """A state change becomes one debounced PATCH; a shape change a new PUT."""
    await setup_entry(options=OPTIONS)
    hass.states.async_set("light.r101", "on", {**LIGHT, "brightness": 100})
    hass.states.async_set("light.r101", "on", {**LIGHT, "brightness": 128})
    hass.states.async_set("switch.lobby", "on", {"friendly_name": "Lobby"})
    await hass.async_block_till_done()
    assert calls(server, "patch", f"{API}/devices/{TV101}/controls/light.r101") == []
    await _flush(hass)
    assert calls(server, "patch", f"{API}/devices/{TV101}/controls/light.r101") == [
        {"state": {"on": True, "brightness": 50}, "available": True}
    ]
    # Unchanged control state: nothing sent.
    hass.states.async_set("light.r101", "on", {**LIGHT, "brightness": 128, "x": 1})
    await _flush(hass)
    assert (
        len(calls(server, "patch", f"{API}/devices/{TV101}/controls/light.r101")) == 1
    )
    # Unavailable.
    hass.states.async_set("light.r101", "unavailable", {})
    await _flush(hass)
    assert calls(server, "patch", f"{API}/devices/{TV101}/controls/light.r101")[-1] == {
        "state": {"on": True, "brightness": 50},
        "available": False,
    }
    assert len(calls(server, "put", f"{API}/devices/{TV101}/controls")) == 1
    # New climate modes = new capabilities: full PUT.
    hass.states.async_set(
        "climate.r101", "heat", {**CLIMATE, "hvac_modes": ["off", "heat"]}
    )
    await _flush(hass)
    assert len(calls(server, "put", f"{API}/devices/{TV101}/controls")) == 2
    # Nothing is ever sent for an entity that is not exposed.
    assert all("lobby" not in str(url) for _m, url, _d, _h in server.mock_calls)


async def test_guest_requests(
    hass: HomeAssistant,
    entities: None,
    server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Valid requests call the HA service and are acked done; the real state is PATCHed."""
    entry = await setup_entry(options=OPTIONS)
    hub = entry.runtime_data
    light_on = async_mock_service(hass, "light", "turn_on")
    light_off = async_mock_service(hass, "light", "turn_off")
    set_temp = async_mock_service(hass, "climate", "set_temperature")
    scene_on = async_mock_service(hass, "scene", "turn_on")

    await hub.async_handle_event(
        _request(1, "light.r101", "set_brightness", {"brightness": 40})
    )
    await hub.async_handle_event(
        _request(2, "climate.r101", "set_temperature", {"temperature": 23.5})
    )
    await hub.async_handle_event(_request(3, "scene.r101", "activate"))
    await hub.async_handle_event(_request(4, "light.r101", "turn_off"))
    await hass.async_block_till_done()

    assert [c.data for c in light_on] == [
        {"entity_id": "light.r101", "brightness": 102}
    ]
    assert [c.data for c in set_temp] == [
        {"entity_id": "climate.r101", "temperature": 23.5}
    ]
    assert [c.data for c in scene_on] == [{"entity_id": "scene.r101"}]
    assert [c.data for c in light_off] == [{"entity_id": "light.r101"}]
    assert _acks(server) == [("1", "done"), ("2", "done"), ("3", "done"), ("4", "done")]

    # The TV changed its state optimistically: the real one goes back even if unchanged.
    await _flush(hass)
    patched = calls(server, "patch", f"{API}/devices/{TV101}/controls/light.r101")
    assert patched == [{"state": {"on": True, "brightness": 100}, "available": True}]


async def test_requests_refused(
    hass: HomeAssistant,
    entities: None,
    server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Non-exposed entities, another TV's entity, bad params, stale requests: failed."""
    entry = await setup_entry(options=OPTIONS)
    hub = entry.runtime_data
    switch_on = async_mock_service(hass, "switch", "turn_on")
    set_temp = async_mock_service(hass, "climate", "set_temperature")

    # Never exposed anywhere.
    await hub.async_handle_event(_request(1, "switch.lobby", "turn_on"))
    # Exposed, but for TV 102: refused when requested from TV 101.
    await hub.async_handle_event(_request(2, "switch.r102", "turn_on", tv_id=TV101))
    # Out of the thermostat's range.
    await hub.async_handle_event(
        _request(3, "climate.r101", "set_temperature", {"temperature": 40})
    )
    # Too old (replayed after HA was down).
    await hub.async_handle_event(
        _request(4, "climate.r101", "set_temperature", {"temperature": 22}, age=600)
    )
    # The right TV: allowed.
    await hub.async_handle_event(_request(5, "switch.r102", "turn_on", tv_id=TV102))
    # Replayed request id: ignored.
    await hub.async_handle_event(_request(5, "switch.r102", "turn_on", tv_id=TV102))
    await hass.async_block_till_done()

    assert [c.data for c in switch_on] == [{"entity_id": "switch.r102"}]
    assert set_temp == []
    assert _acks(server) == [
        ("1", "failed"),
        ("2", "failed"),
        ("3", "failed"),
        ("4", "failed"),
        ("5", "done"),
    ]


async def test_service_failure_acks_failed(
    hass: HomeAssistant,
    entities: None,
    server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """A service that raises is acked failed."""
    from homeassistant.exceptions import HomeAssistantError

    entry = await setup_entry(options=OPTIONS)

    async def _boom(call) -> None:
        raise HomeAssistantError("device offline")

    hass.services.async_register("scene", "turn_on", _boom)
    await entry.runtime_data.async_handle_event(_request(1, "scene.r101", "activate"))
    await hass.async_block_till_done()
    assert _acks(server) == [("1", "failed")]


async def test_options_flow_and_resync(
    hass: HomeAssistant,
    entities: None,
    server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Choosing entities for a TV PUTs it; emptying a TV PUTs an empty list."""
    entry = await setup_entry(options=OPTIONS)
    assert entry.state is ConfigEntryState.LOADED

    # TV 102: replace the switch with the lobby switch, with a label.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["map_tv", "fill_pattern"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "map_tv"}
    )
    assert result["step_id"] == "map_tv"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tv": TV102}
    )
    assert result["step_id"] == "tv_entities"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entities": ["switch.lobby"]}
    )
    assert result["step_id"] == "tv_labels"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"switch.lobby": "Вентилатор"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["controls"][TV102] == {"switch.lobby": "Вентилатор"}
    put102 = calls(server, "put", f"{API}/devices/{TV102}/controls")
    assert put102[-1]["controls"][0]["key"] == "switch.lobby"
    assert put102[-1]["controls"][0]["label"] == "Вентилатор"
    assert (
        len(calls(server, "put", f"{API}/devices/{TV101}/controls")) == 1
    )  # unchanged

    # TV 101: nothing exposed any more.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "map_tv"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tv": TV101}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entities": []}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert TV101 not in entry.options["controls"]
    assert calls(server, "put", f"{API}/devices/{TV101}/controls")[-1] == {
        "controls": []
    }

    # A request for the formerly exposed light is now refused.
    light_on = async_mock_service(hass, "light", "turn_on")
    await entry.runtime_data.async_handle_event(_request(7, "light.r101", "turn_on"))
    await hass.async_block_till_done()
    assert light_on == []
    assert _acks(server)[-1] == ("7", "failed")


def _pending(request_id: int, key: str, action: str, params=None, tv_id=TV101, age=0):
    return {
        "id": request_id,
        "device_id": tv_id,
        "control_id": "c-1",
        "key": key,
        "action": action,
        "params": params or {},
        "created_at": (dt_util.utcnow() - timedelta(seconds=age)).isoformat(),
    }


async def test_pending_requests_on_start_and_reconnect(
    hass: HomeAssistant,
    entities: None,
    server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Requests made while HA was not listening are handled once, with the same checks."""
    scene_on = async_mock_service(hass, "scene", "turn_on")
    switch_on = async_mock_service(hass, "switch", "turn_on")
    server.get(
        f"{API}/control-requests",
        json={
            "requests": [
                _pending(1, "scene.r101", "activate"),
                _pending(2, "scene.r101", "activate", age=120),  # too old
                _pending(3, "switch.lobby", "turn_on"),  # never exposed
                _pending(4, "switch.r102", "turn_on"),  # other TV's entity
            ]
        },
    )
    entry = await setup_entry(options=OPTIONS)
    hub = entry.runtime_data
    assert [c.data for c in scene_on] == [{"entity_id": "scene.r101"}]
    assert switch_on == []
    assert _acks(server) == [
        ("1", "done"),
        ("2", "failed"),
        ("3", "failed"),
        ("4", "failed"),
    ]
    urls = [str(u) for m, u, _d, _h in server.mock_calls if m.lower() == "get"]
    assert f"{API}/control-requests?status=pending" in urls

    # The same request arriving over SSE, and a reconnect re-reading the list: no repeat.
    await hub.async_handle_event(_request(1, "scene.r101", "activate"))
    await hub.bridge.async_process_pending()
    await hass.async_block_till_done()
    assert len(scene_on) == 1
    assert len(_acks(server)) == 4


async def test_pending_requests_endpoint_missing(
    hass: HomeAssistant,
    entities: None,
    server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """An older server without the endpoint (404) is tolerated."""
    server.get(
        f"{API}/control-requests",
        status=404,
        json={"error": {"code": "not_found", "message": "Not found"}},
    )
    entry = await setup_entry(options=OPTIONS)
    assert entry.state is ConfigEntryState.LOADED
    assert _acks(server) == []


# -- thermostats -----------------------------------------------------------------

THERMOSTAT = "climate.room_101_thermostat"
THERMOSTAT_OPTIONS = {"controls": {TV101: {THERMOSTAT: ""}}}
# Switched by mode only: no TURN_ON / TURN_OFF feature.
AC = {
    "friendly_name": "AC 101",
    "hvac_modes": ["off", "heat", "cool", "dry"],
    "fan_modes": ["auto", "low"],
    "swing_modes": ["off", "vertical"],
    "preset_modes": ["none", "eco"],
    "min_temp": 16,
    "max_temp": 30,
    "target_temp_step": 0.5,
    "current_temperature": 21.5,
    "current_humidity": 40,
    "temperature": 22,
    "fan_mode": "auto",
    "swing_mode": "off",
    "preset_mode": "none",
    "hvac_action": "cooling",
    "supported_features": 1 | 8 | 16 | 32,
}


def _without(attributes: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in attributes.items() if k not in keys}


@pytest.fixture
def thermostat_server(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Accept PATCHes of the thermostat and acks."""
    aioclient_mock.patch(f"{API}/devices/{TV101}/controls/{THERMOSTAT}", json={})
    for request_id in range(1, 20):
        aioclient_mock.post(
            f"{API}/control-requests/{request_id}/ack", json={"ok": True}
        )
    return aioclient_mock


async def test_climate_definition_and_unit(
    hass: HomeAssistant,
    thermostat_server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """The thermostat's capabilities, with Home Assistant's temperature unit."""
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set(THERMOSTAT, "cool", AC)
    await setup_entry(options=THERMOSTAT_OPTIONS)
    (put,) = calls(thermostat_server, "put", f"{API}/devices/{TV101}/controls")
    (control,) = put["controls"]
    assert control["capabilities"] == {
        "power": True,
        "target_temperature": True,
        "current_temperature": True,
        "current_humidity": True,
        "min_temp": 16.0,
        "max_temp": 30.0,
        "step": 0.5,
        "unit": "°F",
        "hvac_modes": ["off", "heat", "cool", "dry"],
        "fan_modes": ["auto", "low"],
        "swing_modes": ["off", "vertical"],
        "preset_modes": ["none", "eco"],
    }
    assert control["state"] == {
        "hvac_mode": "cool",
        "hvac_action": "cooling",
        "current_temperature": 21.5,
        "target_temperature": 22.0,
        "current_humidity": 40.0,
        "fan_mode": "auto",
        "swing_mode": "off",
        "preset_mode": "none",
    }


async def test_climate_capabilities_do_not_flap(
    hass: HomeAssistant,
    thermostat_server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Measured values that come and go are PATCHed; the definitions are not re-sent."""
    hass.states.async_set(THERMOSTAT, "cool", AC)
    await setup_entry(options=THERMOSTAT_OPTIONS)
    put_url = f"{API}/devices/{TV101}/controls"
    patch_url = f"{API}/devices/{TV101}/controls/{THERMOSTAT}"
    assert len(calls(thermostat_server, "put", put_url)) == 1

    # Off: Home Assistant nulls the temperature and drops the humidity attribute.
    off = {
        **_without(AC, "current_humidity"),
        "current_temperature": None,
        "temperature": None,
        "hvac_action": "off",
    }
    for _ in range(3):
        hass.states.async_set(THERMOSTAT, "off", off)
        await _flush(hass)
        hass.states.async_set(THERMOSTAT, "cool", AC)
        await _flush(hass)
    # Even the temperature attribute itself going away changes nothing.
    hass.states.async_set(THERMOSTAT, "off", _without(off, "current_temperature"))
    await _flush(hass)
    # Unavailable and back.
    hass.states.async_set(THERMOSTAT, "unavailable", {})
    await _flush(hass)
    hass.states.async_set(THERMOSTAT, "cool", AC)
    await _flush(hass)

    assert len(calls(thermostat_server, "put", put_url)) == 1
    patches = calls(thermostat_server, "patch", patch_url)
    assert len(patches) == 9
    assert patches[0]["state"]["current_temperature"] is None
    assert patches[0]["state"]["current_humidity"] is None
    assert patches[0]["state"]["hvac_mode"] == "off"
    assert patches[1]["state"]["current_humidity"] == 40.0
    assert patches[-2]["available"] is False
    assert patches[-1] == {
        "state": {
            "hvac_mode": "cool",
            "hvac_action": "cooling",
            "current_temperature": 21.5,
            "target_temperature": 22.0,
            "current_humidity": 40.0,
            "fan_mode": "auto",
            "swing_mode": "off",
            "preset_mode": "none",
        },
        "available": True,
    }

    # A real change of what the thermostat can do is still a new PUT.
    hass.states.async_set(THERMOSTAT, "cool", {**AC, "preset_modes": ["none"]})
    await _flush(hass)
    puts = calls(thermostat_server, "put", put_url)
    assert len(puts) == 2
    assert puts[-1]["controls"][0]["capabilities"]["preset_modes"] == ["none"]
    assert puts[-1]["controls"][0]["capabilities"]["current_humidity"] is True


async def test_climate_measured_capability_appears_once(
    hass: HomeAssistant,
    thermostat_server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """A thermostat that was off at start: its humidity shows up later, with one PUT."""
    off = {**_without(AC, "current_humidity"), "current_temperature": None}
    hass.states.async_set(THERMOSTAT, "off", off)
    await setup_entry(options=THERMOSTAT_OPTIONS)
    put_url = f"{API}/devices/{TV101}/controls"
    (first,) = calls(thermostat_server, "put", put_url)
    assert first["controls"][0]["capabilities"]["current_temperature"] is True
    assert first["controls"][0]["capabilities"]["current_humidity"] is False
    for _ in range(3):
        hass.states.async_set(THERMOSTAT, "cool", AC)
        await _flush(hass)
        hass.states.async_set(THERMOSTAT, "off", off)
        await _flush(hass)
    puts = calls(thermostat_server, "put", put_url)
    assert len(puts) == 2
    assert puts[-1]["controls"][0]["capabilities"]["current_humidity"] is True


async def test_climate_guest_requests(
    hass: HomeAssistant,
    thermostat_server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """Every climate action reaches its service; turn_on returns to the last mode."""
    hass.states.async_set(THERMOSTAT, "off", AC)
    entry = await setup_entry(options=THERMOSTAT_OPTIONS)
    hub = entry.runtime_data
    services = {
        name: async_mock_service(hass, "climate", name)
        for name in (
            "turn_on",
            "turn_off",
            "set_temperature",
            "set_hvac_mode",
            "set_fan_mode",
            "set_swing_mode",
            "set_preset_mode",
        )
    }

    def data(name: str) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in c.data.items() if k != "entity_id"}
            for c in services[name]
        ]

    # Nothing seen but `off`: the first mode that is not off.
    await hub.async_handle_event(_request(1, THERMOSTAT, "turn_on"))
    assert data("set_hvac_mode") == [{"hvac_mode": "heat"}]

    # The thermostat runs in `dry`, then is switched off (within one debounce).
    hass.states.async_set(THERMOSTAT, "dry", AC)
    hass.states.async_set(THERMOSTAT, "off", AC)
    await hass.async_block_till_done()
    await hub.async_handle_event(_request(2, THERMOSTAT, "turn_on"))
    assert data("set_hvac_mode")[-1] == {"hvac_mode": "dry"}
    # Unavailable in between does not forget it.
    hass.states.async_set(THERMOSTAT, "unavailable", {})
    hass.states.async_set(THERMOSTAT, "off", AC)
    await hass.async_block_till_done()
    await hub.async_handle_event(_request(3, THERMOSTAT, "turn_on"))
    assert data("set_hvac_mode")[-1] == {"hvac_mode": "dry"}

    await hub.async_handle_event(_request(4, THERMOSTAT, "turn_off"))
    assert data("set_hvac_mode")[-1] == {"hvac_mode": "off"}
    await hub.async_handle_event(
        _request(5, THERMOSTAT, "set_temperature", {"temperature": 22.3})
    )
    assert data("set_temperature") == [{"temperature": 22.5}]
    await hub.async_handle_event(
        _request(6, THERMOSTAT, "set_fan_mode", {"fan_mode": "low"})
    )
    await hub.async_handle_event(
        _request(7, THERMOSTAT, "set_swing_mode", {"swing_mode": "vertical"})
    )
    await hub.async_handle_event(
        _request(8, THERMOSTAT, "set_preset_mode", {"preset_mode": "eco"})
    )
    assert data("set_fan_mode") == [{"fan_mode": "low"}]
    assert data("set_swing_mode") == [{"swing_mode": "vertical"}]
    assert data("set_preset_mode") == [{"preset_mode": "eco"}]
    # Not offered by the thermostat: refused.
    await hub.async_handle_event(
        _request(9, THERMOSTAT, "set_preset_mode", {"preset_mode": "boost"})
    )
    assert len(services["set_preset_mode"]) == 1

    # With the TURN_ON / TURN_OFF features the dedicated services are used.
    hass.states.async_set(THERMOSTAT, "off", {**AC, "supported_features": 387})
    await hass.async_block_till_done()
    await hub.async_handle_event(_request(10, THERMOSTAT, "turn_on"))
    await hub.async_handle_event(_request(11, THERMOSTAT, "turn_off"))
    await hass.async_block_till_done()
    assert data("turn_on") == [{}]
    assert data("turn_off") == [{}]
    assert len(services["set_hvac_mode"]) == 4
    assert all(
        c.data["entity_id"] == THERMOSTAT
        for calls_ in services.values()
        for c in calls_
    )
    assert _acks(thermostat_server) == [
        *[(str(i), "done") for i in range(1, 9)],
        ("9", "failed"),
        ("10", "done"),
        ("11", "done"),
    ]


async def test_climate_memory_follows_the_mapping(
    hass: HomeAssistant,
    thermostat_server: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """What is remembered about a thermostat goes when it is no longer exposed."""
    hass.states.async_set(THERMOSTAT, "cool", AC)
    hass.states.async_set("switch.r102", "off", {})
    entry = await setup_entry(options=THERMOSTAT_OPTIONS)
    bridge = entry.runtime_data.bridge
    assert bridge._last_hvac_mode == {THERMOSTAT: "cool"}
    assert bridge._measured == {THERMOSTAT: {"current_temperature", "current_humidity"}}
    hass.config_entries.async_update_entry(
        entry, options={"controls": {TV102: {"switch.r102": ""}}}
    )
    await hass.async_block_till_done()
    assert bridge._last_hvac_mode == {}
    assert bridge._measured == {}
