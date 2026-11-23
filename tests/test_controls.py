"""HA entity state ⇄ Hotel TV control mapping (pure functions)."""

from __future__ import annotations

import pytest

from custom_components.hotel_tv.controls import (
    ControlRequestError,
    ServiceCall,
    build_control,
    control_capabilities,
    ha_to_pct,
    pct_to_ha,
    remembered_hvac_mode,
    service_call,
    snap_temperature,
)

CLIMATE_ATTRS = {
    "friendly_name": "Room 101 AC",
    "hvac_modes": ["off", "heat", "cool", "auto"],
    "fan_modes": ["auto", "low", "high"],
    "min_temp": 16,
    "max_temp": 30,
    "target_temp_step": 0.5,
    "current_temperature": 22.4,
    "temperature": 23,
    "fan_mode": "auto",
    "supported_features": 1 | 8,  # TARGET_TEMPERATURE | FAN_MODE
}
# Real entities (Home Assistant states of the hotel).
ROOM_204 = {
    "friendly_name": "Room 204 Thermostat",
    "hvac_modes": ["off", "cool", "heat"],
    "min_temp": 15,
    "max_temp": 30,
    "target_temp_step": 0.1,
    "current_temperature": 22.5,
    "temperature": 22,
    "target_temp_high": 18,
    "target_temp_low": 22,
    "hvac_action": "idle",
    "supported_features": 387,  # TARGET_TEMPERATURE | RANGE | TURN_OFF | TURN_ON
}
SAUNA = {
    "friendly_name": "Sauna",
    "hvac_modes": ["off", "heat"],
    "min_temp": 20,
    "max_temp": 100,
    "target_temp_step": 0.5,
    "current_temperature": None,
    "temperature": None,
    "hvac_action": "off",
    "supported_features": 385,  # TARGET_TEMPERATURE | TURN_OFF | TURN_ON
}
# A split AC with everything, switched by mode only (no TURN_ON / TURN_OFF).
SPLIT = {
    "hvac_modes": ["off", "heat", "cool", "dry"],
    "fan_modes": ["auto", "low", "high"],
    "swing_modes": ["off", "vertical"],
    "preset_modes": ["none", "eco"],
    "min_temp": 16,
    "max_temp": 30,
    "target_temp_step": 1,
    "current_temperature": 24,
    "current_humidity": 41,
    "temperature": 23,
    "fan_mode": "low",
    "swing_mode": "off",
    "preset_mode": "eco",
    "hvac_action": "cooling",
    "supported_features": 1 | 8 | 16 | 32,
}
DIMMABLE = {"friendly_name": "Ceiling", "supported_color_modes": ["brightness"]}
ONOFF = {"friendly_name": "Lamp", "supported_color_modes": ["onoff"]}


def test_climate_control() -> None:
    """State, capabilities and label of a thermostat."""
    control = build_control("climate.r101", "cool", CLIMATE_ATTRS, position=2)
    assert control == {
        "key": "climate.r101",
        "kind": "climate",
        "label": "Room 101 AC",
        "icon": None,
        "position": 2,
        "state": {
            "hvac_mode": "cool",
            "hvac_action": None,
            "current_temperature": 22.4,
            "target_temperature": 23.0,
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
            "hvac_modes": ["off", "heat", "cool", "auto"],
            "fan_modes": ["auto", "low", "high"],
            "swing_modes": [],
            "preset_modes": [],
        },
        "available": True,
    }


def test_climate_real_room_thermostat() -> None:
    """Room 204: heat/cool with on/off, a setpoint, no fan/swing/preset."""
    control = build_control(
        "climate.room_204_thermostat_climate_controller_2", "heat", ROOM_204
    )
    assert control["state"] == {
        "hvac_mode": "heat",
        "hvac_action": "idle",
        "current_temperature": 22.5,
        "target_temperature": 22.0,
        "current_humidity": None,
        "fan_mode": None,
        "swing_mode": None,
        "preset_mode": None,
    }
    assert control["capabilities"] == {
        "power": True,
        "target_temperature": True,
        "current_temperature": True,
        "current_humidity": False,
        "min_temp": 15.0,
        "max_temp": 30.0,
        "step": 0.1,
        "unit": "°C",
        "hvac_modes": ["off", "cool", "heat"],
        "fan_modes": [],
        "swing_modes": [],
        "preset_modes": [],
    }


def test_climate_real_sauna_off() -> None:
    """The sauna while off: no temperatures, but it still measures and can be set."""
    control = build_control("climate.sauna", "off", SAUNA)
    assert control["available"] is True
    assert control["state"] == {
        "hvac_mode": "off",
        "hvac_action": "off",
        "current_temperature": None,
        "target_temperature": None,
        "current_humidity": None,
        "fan_mode": None,
        "swing_mode": None,
        "preset_mode": None,
    }
    assert control["capabilities"] == {
        "power": True,
        "target_temperature": True,
        "current_temperature": True,  # the attribute is there, its value is not
        "current_humidity": False,
        "min_temp": 20.0,
        "max_temp": 100.0,
        "step": 0.5,
        "unit": "°C",
        "hvac_modes": ["off", "heat"],
        "fan_modes": [],
        "swing_modes": [],
        "preset_modes": [],
    }


def test_climate_full_featured() -> None:
    """Fan, swing, preset and humidity of a split AC; °F from Home Assistant."""
    control = build_control("climate.split", "cool", SPLIT, unit="°F")
    assert control["state"] == {
        "hvac_mode": "cool",
        "hvac_action": "cooling",
        "current_temperature": 24.0,
        "target_temperature": 23.0,
        "current_humidity": 41.0,
        "fan_mode": "low",
        "swing_mode": "off",
        "preset_mode": "eco",
    }
    caps = control["capabilities"]
    assert caps["unit"] == "°F"
    assert caps["current_humidity"] is True
    assert caps["fan_modes"] == ["auto", "low", "high"]
    assert caps["swing_modes"] == ["off", "vertical"]
    assert caps["preset_modes"] == ["none", "eco"]
    assert caps["power"] is True  # `off` is one of the modes


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        # power: `off` among the modes, or both TURN_ON and TURN_OFF.
        ({"hvac_modes": ["off", "heat"]}, {"power": True}),
        ({"hvac_modes": ["heat"]}, {"power": False}),
        ({"hvac_modes": ["heat"], "supported_features": 128}, {"power": False}),
        ({"hvac_modes": ["heat"], "supported_features": 256}, {"power": False}),
        ({"hvac_modes": ["heat"], "supported_features": 384}, {"power": True}),
        # target_temperature: the single setpoint only.
        ({"supported_features": 1}, {"target_temperature": True}),
        ({"supported_features": 3}, {"target_temperature": True}),
        ({"supported_features": 2}, {"target_temperature": False}),  # range only
        ({"supported_features": 4}, {"target_temperature": False}),  # humidity
        # Mode lists only with their feature bit.
        ({"fan_modes": ["low"]}, {"fan_modes": []}),
        ({"fan_modes": ["low"], "supported_features": 8}, {"fan_modes": ["low"]}),
        ({"preset_modes": ["eco"], "supported_features": 8}, {"preset_modes": []}),
        (
            {"preset_modes": ["eco"], "supported_features": 16},
            {"preset_modes": ["eco"]},
        ),
        ({"swing_modes": ["on"], "supported_features": 16}, {"swing_modes": []}),
        ({"swing_modes": ["on"], "supported_features": 32}, {"swing_modes": ["on"]}),
        # Measured values: the attribute key, whatever its value.
        ({}, {"current_temperature": False, "current_humidity": False}),
        ({"current_temperature": None}, {"current_temperature": True}),
        ({"current_humidity": 40}, {"current_humidity": True}),
        # Odd values never break the definition.
        ({"supported_features": "x", "target_temp_step": 0}, {"step": 0.5}),
        ({"target_temp_step": -1}, {"step": 0.5}),
    ],
)
def test_climate_capabilities_from_features(attributes: dict, expected: dict) -> None:
    """Each capability follows the entity's features and attributes."""
    caps = control_capabilities("climate", attributes)
    assert {key: caps[key] for key in expected} == expected


def test_climate_unit() -> None:
    """Home Assistant's unit; anything unexpected is °C."""
    assert control_capabilities("climate", {})["unit"] == "°C"
    assert control_capabilities("climate", {}, unit="°F")["unit"] == "°F"
    assert control_capabilities("climate", {}, unit="K")["unit"] == "°C"
    # Other kinds are not affected by the unit.
    assert control_capabilities("light", ONOFF, unit="°F") == {"dimmable": False}
    assert control_capabilities("switch", {}, unit="°F") == {}


def test_climate_defaults_and_label_override() -> None:
    """Missing limits get defaults; the label override wins over the HA name."""
    control = build_control(
        "climate.r101", "heat", {"friendly_name": "AC"}, label="  Климатик "
    )
    assert control["label"] == "Климатик"
    assert control["capabilities"] == {
        "power": False,
        "target_temperature": False,
        "current_temperature": False,
        "current_humidity": False,
        "min_temp": 7.0,
        "max_temp": 35.0,
        "step": 0.5,
        "unit": "°C",
        "hvac_modes": [],
        "fan_modes": [],
        "swing_modes": [],
        "preset_modes": [],
    }
    assert control["state"]["target_temperature"] is None


@pytest.mark.parametrize(
    ("ha", "pct"), [(0, 0), (1, 0), (3, 1), (128, 50), (255, 100), (None, 0)]
)
def test_brightness_to_pct(ha: int | None, pct: int) -> None:
    """0–255 → 0–100."""
    assert ha_to_pct(ha) == pct


@pytest.mark.parametrize(("pct", "ha"), [(0, 0), (1, 3), (50, 128), (100, 255)])
def test_pct_to_brightness(pct: int, ha: int) -> None:
    """0–100 → 0–255."""
    assert pct_to_ha(pct) == ha


def test_light_states() -> None:
    """Dimmable on / off; an on/off light is 100 when on."""
    on = build_control("light.r101", "on", {**DIMMABLE, "brightness": 64})
    assert on["state"] == {"on": True, "brightness": 25}
    assert on["capabilities"] == {"dimmable": True}
    off = build_control("light.r101", "off", {**DIMMABLE, "brightness": None})
    assert off["state"] == {"on": False, "brightness": 0}
    lamp = build_control("light.lamp", "on", ONOFF)
    assert lamp["state"] == {"on": True, "brightness": 100}
    assert lamp["capabilities"] == {"dimmable": False}


def test_light_colour() -> None:
    """A colour light: hue / saturation, warm–cold white within its range."""
    attrs = {
        "supported_color_modes": ["hs", "color_temp"],
        "color_mode": "hs",
        "brightness": 255,
        "hs_color": [30.4, 80.6],
        "color_temp_kelvin": None,
        "min_color_temp_kelvin": 2200,
        "max_color_temp_kelvin": 6500,
    }
    control = build_control("light.rgb", "on", attrs)
    assert control["capabilities"] == {
        "dimmable": True,
        "color": True,
        "color_temp": True,
        "min_kelvin": 2200,
        "max_kelvin": 6500,
    }
    assert control["state"]["hue"] == 30
    assert control["state"]["saturation"] == 81
    assert control["state"]["color_mode"] == "color"
    assert service_call(
        "light.rgb", "set_color", {"hue": 120, "saturation": 100}, attrs
    ) == ServiceCall("light", "turn_on", {"entity_id": "light.rgb", "hs_color": [120, 100]})
    assert service_call(
        "light.rgb", "set_color_temp", {"kelvin": 3000}, attrs
    ) == ServiceCall(
        "light", "turn_on", {"entity_id": "light.rgb", "color_temp_kelvin": 3000}
    )
    with pytest.raises(ControlRequestError):
        service_call("light.rgb", "set_color_temp", {"kelvin": 9000}, attrs)
    with pytest.raises(ControlRequestError):
        service_call("light.lamp", "set_color", {"hue": 1, "saturation": 1}, ONOFF)


def test_cover_position_capability() -> None:
    """Only an open / close cover says it has no position."""
    assert control_capabilities("cover", {"supported_features": 15}) == {}
    assert control_capabilities("cover", {"supported_features": 3}) == {"position": False}
    assert control_capabilities("cover", {}) == {}


def test_fan_lock_media() -> None:
    """Fans (speed), locks, media players (play / pause, volume)."""
    fan = build_control(
        "fan.ceiling", "on", {"percentage": 66, "percentage_step": 33.3, "supported_features": 1}
    )
    assert fan["kind"] == "fan"
    assert fan["state"] == {"on": True, "percentage": 66}
    assert fan["capabilities"] == {"speed": True, "percentage_step": 33.3}
    assert service_call(
        "fan.ceiling", "set_percentage", {"percentage": 33}, {"supported_features": 1}
    ) == ServiceCall("fan", "set_percentage", {"entity_id": "fan.ceiling", "percentage": 33})
    assert service_call("fan.ceiling", "set_percentage", {"percentage": 0}, {"supported_features": 1}).service == "turn_off"
    with pytest.raises(ControlRequestError):
        service_call("fan.ceiling", "set_percentage", {"percentage": 50}, {})

    lock = build_control("lock.front_door", "locked", {})
    assert lock["kind"] == "lock"
    assert lock["state"] == {"locked": True, "state": "locked"}
    assert service_call("lock.front_door", "unlock", {}, {}) == ServiceCall(
        "lock", "unlock", {"entity_id": "lock.front_door"}
    )

    attrs = {"supported_features": 1 | 4 | 16384, "volume_level": 0.35}
    media = build_control("media_player.speaker", "playing", attrs)
    assert media["kind"] == "media"
    assert media["state"] == {"state": "playing", "volume": 35, "muted": False}
    assert media["capabilities"] == {"play_pause": True, "volume": True}
    assert service_call("media_player.speaker", "set_volume", {"volume": 40}, attrs) == ServiceCall(
        "media_player", "volume_set", {"entity_id": "media_player.speaker", "volume_level": 0.4}
    )
    assert service_call("media_player.speaker", "play_pause", {}, attrs).service == "media_play_pause"


def test_switch_cover_scene() -> None:
    """The simple kinds."""
    assert build_control("switch.fan", "on", {})["state"] == {"on": True}
    assert build_control("cover.blind", "open", {"current_position": 35})["state"] == {
        "position": 35
    }
    assert build_control("cover.door", "open", {})["state"] == {"position": 100}
    assert build_control("cover.door", "closed", {})["state"] == {"position": 0}
    scene = build_control("scene.relax", "unknown", {"friendly_name": "Relax"})
    assert scene["state"] == {}
    assert scene["available"] is True  # a never-activated scene is "unknown"


def test_unavailable_and_missing() -> None:
    """Unavailable and not-yet-existing entities are sent unavailable."""
    assert build_control("light.x", "unavailable", {})["available"] is False
    missing = build_control("light.x", None, None)
    assert missing["available"] is False
    assert missing["label"] == "light.x"


def test_not_exposable() -> None:
    """Domains outside the control table are refused."""
    with pytest.raises(ValueError):
        build_control("vacuum.robot", "docked", {})


# -- guest requests → service calls ---------------------------------------------


def test_climate_requests() -> None:
    """Temperature within limits, modes from the entity's lists."""
    assert service_call(
        "climate.r101", "set_temperature", {"temperature": 21.5}, CLIMATE_ATTRS
    ) == ServiceCall(
        "climate", "set_temperature", {"entity_id": "climate.r101", "temperature": 21.5}
    )
    assert service_call(
        "climate.r101", "set_hvac_mode", {"hvac_mode": "heat"}, CLIMATE_ATTRS
    ) == ServiceCall(
        "climate", "set_hvac_mode", {"entity_id": "climate.r101", "hvac_mode": "heat"}
    )
    assert (
        service_call(
            "climate.r101", "set_fan_mode", {"fan_mode": "low"}, CLIMATE_ATTRS
        ).service
        == "set_fan_mode"
    )


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("set_temperature", {"temperature": 35}),
        ("set_temperature", {"temperature": "hot"}),
        ("set_temperature", {"temperature": True}),
        ("set_temperature", {}),
        ("set_hvac_mode", {"hvac_mode": "dry"}),
        ("set_fan_mode", {"fan_mode": "turbo"}),
        ("set_fan_mode", {}),
        ("set_swing_mode", {"swing_mode": "vertical"}),  # no SWING_MODE feature
        ("set_preset_mode", {"preset_mode": "eco"}),  # no PRESET_MODE feature
        ("activate", {}),
        ("set_brightness", {"brightness": 50}),
    ],
)
def test_climate_refused(action: str, params: dict) -> None:
    """Out of range, unknown modes and foreign actions are refused."""
    with pytest.raises(ControlRequestError):
        service_call("climate.r101", action, params, CLIMATE_ATTRS)


def _climate(action: str, params: dict | None, attributes: dict, **kwargs) -> tuple:
    call = service_call("climate.x", action, params, attributes, **kwargs)
    assert call.domain == "climate"
    assert call.data.pop("entity_id") == "climate.x"
    return call.service, call.data


def test_climate_turn_off() -> None:
    """climate.turn_off with the feature, else the `off` mode, else refused."""
    assert _climate("turn_off", {}, ROOM_204) == ("turn_off", {})
    assert _climate("turn_off", {}, SAUNA) == ("turn_off", {})
    assert _climate("turn_off", None, SPLIT) == ("set_hvac_mode", {"hvac_mode": "off"})
    with pytest.raises(ControlRequestError):
        _climate("turn_off", {}, {"hvac_modes": ["heat", "cool"]})
    # TURN_ON alone does not turn anything off.
    with pytest.raises(ControlRequestError):
        _climate("turn_off", {}, {"hvac_modes": ["heat"], "supported_features": 256})


def test_climate_turn_on() -> None:
    """climate.turn_on with the feature; else back to the last mode, else the first."""
    assert _climate("turn_on", {}, ROOM_204) == ("turn_on", {})
    assert _climate("turn_on", {}, SAUNA, last_hvac_mode="heat") == ("turn_on", {})
    # No TURN_ON feature: the mode the bridge saw last.
    assert _climate("turn_on", {}, SPLIT, last_hvac_mode="cool") == (
        "set_hvac_mode",
        {"hvac_mode": "cool"},
    )
    # Nothing remembered, or something the entity no longer offers: the first
    # mode that is not `off`.
    for last in (None, "off", "fan_only"):
        assert _climate("turn_on", {}, SPLIT, last_hvac_mode=last) == (
            "set_hvac_mode",
            {"hvac_mode": "heat"},
        )
    for attributes in ({"hvac_modes": ["off"]}, {}):
        with pytest.raises(ControlRequestError):
            _climate("turn_on", {}, attributes, last_hvac_mode="heat")


def test_climate_swing_and_preset() -> None:
    """Swing and preset modes from the entity's lists only."""
    assert _climate("set_swing_mode", {"swing_mode": "vertical"}, SPLIT) == (
        "set_swing_mode",
        {"swing_mode": "vertical"},
    )
    assert _climate("set_preset_mode", {"preset_mode": "none"}, SPLIT) == (
        "set_preset_mode",
        {"preset_mode": "none"},
    )
    assert _climate("set_fan_mode", {"fan_mode": "high"}, SPLIT) == (
        "set_fan_mode",
        {"fan_mode": "high"},
    )
    assert _climate("set_hvac_mode", {"hvac_mode": "dry"}, SPLIT) == (
        "set_hvac_mode",
        {"hvac_mode": "dry"},
    )
    for action, params in (
        ("set_swing_mode", {"swing_mode": "horizontal"}),
        ("set_swing_mode", {"swing_mode": None}),
        ("set_preset_mode", {"preset_mode": "boost"}),
        ("set_preset_mode", {"preset_mode": 1}),
        ("set_preset_mode", {}),
        # The list is there but the feature bit is not: refused.
        ("set_swing_mode", {"swing_mode": "vertical", "_features": 1 | 8 | 16}),
        ("set_preset_mode", {"preset_mode": "eco", "_features": 1 | 8 | 32}),
        ("set_fan_mode", {"fan_mode": "low", "_features": 1}),
    ):
        attributes = dict(SPLIT)
        if "_features" in params:
            attributes["supported_features"] = params.pop("_features")
        with pytest.raises(ControlRequestError):
            _climate(action, params, attributes)


@pytest.mark.parametrize(
    ("attributes", "asked", "sent"),
    [
        (ROOM_204, 22.5, 22.5),  # step 0.1
        (ROOM_204, 22.449, 22.4),
        (ROOM_204, 22.46, 22.5),
        (ROOM_204, 30, 30.0),
        (ROOM_204, 15, 15.0),
        (SAUNA, 80.2, 80.0),  # step 0.5
        (SAUNA, 80.25, 80.5),
        (SAUNA, 99.9, 100.0),
        (SPLIT, 22.5, 23.0),  # step 1: halves go up, like the TV
        (SPLIT, 23.4, 23.0),
        # No step stated: the value as asked.
        ({"supported_features": 1, "min_temp": 16, "max_temp": 30}, 21.3, 21.3),
        # The grid starts at the minimum (as on the TV), not at zero.
        (
            {
                "supported_features": 1,
                "min_temp": 16.5,
                "max_temp": 30,
                "target_temp_step": 1,
            },
            18.4,
            18.5,
        ),
        # Snapping never leaves the range.
        (
            {
                "supported_features": 1,
                "min_temp": 16,
                "max_temp": 30.4,
                "target_temp_step": 1,
            },
            30.4,
            30.0,
        ),
        (
            {
                "supported_features": 1,
                "min_temp": 16,
                "max_temp": 30.6,
                "target_temp_step": 1,
            },
            30.6,
            30.6,
        ),
    ],
)
def test_climate_temperature_snaps_to_step(
    attributes: dict, asked: float, sent: float
) -> None:
    """The temperature goes to the entity on its own step grid."""
    service, data = _climate("set_temperature", {"temperature": asked}, attributes)
    assert service == "set_temperature"
    assert data == {"temperature": sent}
    assert isinstance(data["temperature"], float)


def test_climate_temperature_refused() -> None:
    """Outside the entity's range, or no single setpoint to set."""
    for temperature in (14.9, 30.1, None, "22", float("nan")):
        if temperature == "22":
            # A numeric string is a number (as before).
            assert _climate("set_temperature", {"temperature": "22"}, ROOM_204)[1] == {
                "temperature": 22.0
            }
            continue
        with pytest.raises(ControlRequestError):
            _climate("set_temperature", {"temperature": temperature}, ROOM_204)
    # Range-only thermostat (heat_cool with low/high): no single setpoint.
    range_only = {**ROOM_204, "supported_features": 2 | 128 | 256}
    with pytest.raises(ControlRequestError):
        _climate("set_temperature", {"temperature": 22}, range_only)
    # No features at all.
    with pytest.raises(ControlRequestError):
        _climate("set_temperature", {"temperature": 22}, {"min_temp": 16})


def test_snap_and_remembered_mode() -> None:
    """The two small helpers."""
    assert snap_temperature(21.26, 15, 0.1) == 21.3
    assert snap_temperature(21.26, 15, 0.5) == 21.5
    assert snap_temperature(21.26, 15, None) == 21.26
    assert snap_temperature(21.26, 15, 0) == 21.26
    assert remembered_hvac_mode("heat") == "heat"
    for state in ("off", "unavailable", "unknown", "", None):
        assert remembered_hvac_mode(state) is None


def test_light_requests() -> None:
    """Brightness 0–100 becomes 0–255; 0 turns the light off."""
    assert service_call(
        "light.r101", "set_brightness", {"brightness": 50}, DIMMABLE
    ) == ServiceCall("light", "turn_on", {"entity_id": "light.r101", "brightness": 128})
    assert service_call(
        "light.r101", "set_brightness", {"brightness": 0}, DIMMABLE
    ) == ServiceCall("light", "turn_off", {"entity_id": "light.r101"})
    assert service_call("light.r101", "turn_on", {}, DIMMABLE) == ServiceCall(
        "light", "turn_on", {"entity_id": "light.r101"}
    )
    assert service_call("light.r101", "turn_on", {"brightness": 1}, DIMMABLE).data == {
        "entity_id": "light.r101",
        "brightness": 3,
    }
    assert service_call("light.r101", "turn_off", None, DIMMABLE).service == "turn_off"
    # An on/off light ignores a brightness on turn_on but refuses set_brightness.
    assert service_call("light.lamp", "turn_on", {"brightness": 40}, ONOFF).data == {
        "entity_id": "light.lamp"
    }
    with pytest.raises(ControlRequestError):
        service_call("light.lamp", "set_brightness", {"brightness": 40}, ONOFF)
    with pytest.raises(ControlRequestError):
        service_call("light.r101", "set_brightness", {"brightness": 101}, DIMMABLE)


def test_switch_cover_scene_requests() -> None:
    """Switch on/off, cover position (or nearest end), scene activation."""
    assert service_call("switch.fan", "turn_on", {}, {}) == ServiceCall(
        "switch", "turn_on", {"entity_id": "switch.fan"}
    )
    with pytest.raises(ControlRequestError):
        service_call("switch.fan", "activate", {}, {})
    assert service_call(
        "cover.blind", "set_position", {"position": 30}, {"supported_features": 15}
    ) == ServiceCall(
        "cover", "set_cover_position", {"entity_id": "cover.blind", "position": 30}
    )
    assert service_call(
        "cover.door", "set_position", {"position": 80}, {"supported_features": 3}
    ) == ServiceCall("cover", "open_cover", {"entity_id": "cover.door"})
    assert service_call(
        "cover.door", "set_position", {"position": 20}, {"supported_features": 3}
    ) == ServiceCall("cover", "close_cover", {"entity_id": "cover.door"})
    with pytest.raises(ControlRequestError):
        service_call("cover.door", "set_position", {"position": 20}, {})
    assert service_call("scene.relax", "activate", {}, {}) == ServiceCall(
        "scene", "turn_on", {"entity_id": "scene.relax"}
    )


def test_unknown_domain_request() -> None:
    """Nothing is ever called for a domain outside the table."""
    with pytest.raises(ControlRequestError):
        service_call("lock.front_door", "turn_on", {}, {})
