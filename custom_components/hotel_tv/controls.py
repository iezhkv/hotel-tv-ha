"""Mapping between Home Assistant entities and Hotel TV room controls.

See docs/CONTRACTS.md "Control state shapes (HA ⇄ TV)". Pure functions on plain
values (entity id, state string, attributes dict) so they are testable without a
running Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from typing import Any

from .const import CONTROL_DOMAINS

UNAVAILABLE = "unavailable"

# homeassistant.components.cover.CoverEntityFeature
COVER_OPEN = 1
COVER_CLOSE = 2
COVER_SET_POSITION = 4

# homeassistant.components.climate.ClimateEntityFeature
CLIMATE_TARGET_TEMPERATURE = 1
CLIMATE_TARGET_TEMPERATURE_RANGE = 2
CLIMATE_FAN_MODE = 8
CLIMATE_PRESET_MODE = 16
CLIMATE_SWING_MODE = 32
CLIMATE_TURN_OFF = 128
CLIMATE_TURN_ON = 256

# homeassistant.components.light.ColorMode values that carry no brightness.
_NO_BRIGHTNESS_MODES = frozenset({"onoff", "unknown"})

DEFAULT_CLIMATE = {"min_temp": 7.0, "max_temp": 35.0, "step": 0.5}
DEFAULT_UNIT = "°C"
HVAC_OFF = "off"
# States that are not an hvac mode.
_NO_MODE = frozenset({UNAVAILABLE, "unknown", ""})

# Capabilities that say "the entity measures this". Home Assistant drops the
# attribute (or nulls it) while the value is unknown, so the bridge latches them.
CLIMATE_SENSOR_CAPABILITIES = ("current_temperature", "current_humidity")


class ControlRequestError(ValueError):
    """A guest request that cannot be executed (bad action or parameters)."""


@dataclass(frozen=True, slots=True)
class ServiceCall:
    """A Home Assistant service call that carries out a guest request."""

    domain: str
    service: str
    data: dict[str, Any] = field(default_factory=dict)


def control_kind(entity_id: str) -> str | None:
    """The control kind of an entity, None when its domain cannot be exposed."""
    return CONTROL_DOMAINS.get(entity_id.partition(".")[0])


def ha_to_pct(brightness: Any) -> int:
    """HA brightness 0–255 → 0–100."""
    try:
        value = float(brightness)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, round(value * 100 / 255)))


def pct_to_ha(pct: Any) -> int:
    """0–100 → HA brightness 0–255."""
    value = _number(pct, "brightness")
    return max(0, min(255, round(value * 255 / 100)))


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ControlRequestError(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise ControlRequestError(f"{name} must be a number") from err
    if not math.isfinite(number):
        raise ControlRequestError(f"{name} must be a number")
    return number


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value]


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _features(attributes: Mapping[str, Any]) -> int:
    try:
        return int(attributes.get("supported_features") or 0)
    except (TypeError, ValueError):
        return 0


def remembered_hvac_mode(state: str | None) -> str | None:
    """The state as a mode worth returning to on `turn_on` (None for off/unknown)."""
    if state is None or state in _NO_MODE or state == HVAC_OFF:
        return None
    return state


def snap_temperature(value: float, minimum: float, step: float | None) -> float:
    """`value` on the entity's step grid (anchored at its minimum, like the TV's)."""
    if not step or step <= 0:
        return round(value, 2)
    steps = math.floor((value - minimum) / step + 0.5)
    return round(minimum + steps * step, 2)


def light_dimmable(attributes: Mapping[str, Any]) -> bool:
    """The light supports brightness (any color mode besides on/off)."""
    modes = attributes.get("supported_color_modes")
    if not modes:
        return attributes.get("brightness") is not None
    return any(str(mode) not in _NO_BRIGHTNESS_MODES for mode in modes)


def control_state(
    kind: str, state: str | None, attributes: Mapping[str, Any]
) -> dict[str, Any]:
    """The control `state` object of an entity."""
    if kind == "climate":
        return {
            "hvac_mode": state if state not in (None, UNAVAILABLE, "unknown") else None,
            "hvac_action": _str_or_none(attributes.get("hvac_action")),
            "current_temperature": _float_or_none(
                attributes.get("current_temperature")
            ),
            "target_temperature": _float_or_none(attributes.get("temperature")),
            "current_humidity": _float_or_none(attributes.get("current_humidity")),
            "fan_mode": _str_or_none(attributes.get("fan_mode")),
            "swing_mode": _str_or_none(attributes.get("swing_mode")),
            "preset_mode": _str_or_none(attributes.get("preset_mode")),
        }
    if kind == "light":
        on = state == "on"
        if not on:
            brightness = 0
        elif light_dimmable(attributes) and attributes.get("brightness") is not None:
            brightness = ha_to_pct(attributes.get("brightness"))
        else:
            brightness = 100
        return {"on": on, "brightness": brightness}
    if kind == "switch":
        return {"on": state == "on"}
    if kind == "cover":
        position = _float_or_none(attributes.get("current_position"))
        if position is None:
            position = 100 if state in ("open", "opening") else 0
        return {"position": max(0, min(100, round(position)))}
    return {}


def control_capabilities(
    kind: str, attributes: Mapping[str, Any], *, unit: str = DEFAULT_UNIT
) -> dict[str, Any]:
    """The control `capabilities` object of an entity.

    Climate (docs/CONTRACTS.md "Climate controls"): what the thermostat can do,
    from its `supported_features` and attributes. `unit` is Home Assistant's
    temperature unit. A thermostat with only a low/high range
    (TARGET_TEMPERATURE_RANGE without TARGET_TEMPERATURE) has no single setpoint
    the TV could change: `target_temperature` is false.
    """
    if kind == "climate":
        features = _features(attributes)
        min_temp = _float_or_none(attributes.get("min_temp"))
        max_temp = _float_or_none(attributes.get("max_temp"))
        step = _float_or_none(attributes.get("target_temp_step"))
        hvac_modes = _str_list(attributes.get("hvac_modes"))

        def modes(key: str, feature: int) -> list[str]:
            return _str_list(attributes.get(key)) if features & feature else []

        return {
            "power": HVAC_OFF in hvac_modes
            or bool(features & CLIMATE_TURN_ON and features & CLIMATE_TURN_OFF),
            "target_temperature": bool(features & CLIMATE_TARGET_TEMPERATURE),
            # The attribute is there (its value may be None for a moment).
            "current_temperature": "current_temperature" in attributes,
            "current_humidity": "current_humidity" in attributes,
            "min_temp": DEFAULT_CLIMATE["min_temp"] if min_temp is None else min_temp,
            "max_temp": DEFAULT_CLIMATE["max_temp"] if max_temp is None else max_temp,
            "step": step if step and step > 0 else DEFAULT_CLIMATE["step"],
            "unit": "°F" if unit == "°F" else DEFAULT_UNIT,
            "hvac_modes": hvac_modes,
            "fan_modes": modes("fan_modes", CLIMATE_FAN_MODE),
            "swing_modes": modes("swing_modes", CLIMATE_SWING_MODE),
            "preset_modes": modes("preset_modes", CLIMATE_PRESET_MODE),
        }
    if kind == "light":
        return {"dimmable": light_dimmable(attributes)}
    return {}


def build_control(
    entity_id: str,
    state: str | None,
    attributes: Mapping[str, Any] | None,
    *,
    label: str | None = None,
    position: int = 0,
    unit: str = DEFAULT_UNIT,
) -> dict[str, Any]:
    """The full control definition for `PUT /rooms/{id}/controls`.

    `state` None means the entity does not exist (yet): it is sent unavailable.
    `unit` is Home Assistant's temperature unit (climate capabilities).
    """
    kind = control_kind(entity_id)
    if kind is None:
        raise ValueError(f"{entity_id} cannot be exposed as a room control")
    attributes = attributes or {}
    return {
        "key": entity_id,
        "kind": kind,
        "label": (label or "").strip()
        or str(attributes.get("friendly_name") or entity_id),
        "icon": None,
        "position": position,
        "state": control_state(kind, state, attributes),
        "capabilities": control_capabilities(kind, attributes, unit=unit),
        "available": state is not None and state != UNAVAILABLE,
    }


def service_call(
    entity_id: str,
    action: str,
    params: Mapping[str, Any] | None,
    attributes: Mapping[str, Any] | None,
    *,
    last_hvac_mode: str | None = None,
) -> ServiceCall:
    """The service call for a guest's `control.requested`; ControlRequestError if invalid.

    `attributes` are the entity's current ones: limits and modes are checked
    against them, not against what the TV believed. `last_hvac_mode` is the last
    mode other than `off` the thermostat was seen in (for `turn_on` on a
    thermostat without the TURN_ON feature).
    """
    kind = control_kind(entity_id)
    params = params if isinstance(params, Mapping) else {}
    attributes = attributes or {}
    target = {"entity_id": entity_id}

    if kind == "climate":
        caps = control_capabilities("climate", attributes)
        features = _features(attributes)
        hvac_modes: list[str] = caps["hvac_modes"]
        if action == "turn_off":
            if features & CLIMATE_TURN_OFF:
                return ServiceCall("climate", "turn_off", target)
            if HVAC_OFF in hvac_modes:
                return ServiceCall(
                    "climate", "set_hvac_mode", {**target, "hvac_mode": HVAC_OFF}
                )
            raise ControlRequestError("thermostat cannot be turned off")
        if action == "turn_on":
            if features & CLIMATE_TURN_ON:
                return ServiceCall("climate", "turn_on", target)
            running = [mode for mode in hvac_modes if mode != HVAC_OFF]
            if not running:
                raise ControlRequestError("thermostat cannot be turned on")
            mode = last_hvac_mode if last_hvac_mode in running else running[0]
            return ServiceCall(
                "climate", "set_hvac_mode", {**target, "hvac_mode": mode}
            )
        if action == "set_temperature":
            if not caps["target_temperature"]:
                raise ControlRequestError("thermostat has no single target temperature")
            temperature = _number(params.get("temperature"), "temperature")
            if not caps["min_temp"] <= temperature <= caps["max_temp"]:
                raise ControlRequestError("temperature out of range")
            temperature = snap_temperature(
                temperature,
                caps["min_temp"],
                _float_or_none(attributes.get("target_temp_step")),
            )
            temperature = max(caps["min_temp"], min(caps["max_temp"], temperature))
            return ServiceCall(
                "climate", "set_temperature", {**target, "temperature": temperature}
            )
        for name, modes in (
            ("hvac_mode", hvac_modes),
            ("fan_mode", caps["fan_modes"]),
            ("swing_mode", caps["swing_modes"]),
            ("preset_mode", caps["preset_modes"]),
        ):
            if action == f"set_{name}":
                mode = params.get(name)
                if not isinstance(mode, str) or mode not in modes:
                    raise ControlRequestError(f"unsupported {name}")
                return ServiceCall("climate", f"set_{name}", {**target, name: mode})

    elif kind == "light":
        dimmable = light_dimmable(attributes)
        if action == "turn_off":
            return ServiceCall("light", "turn_off", target)
        if action in ("turn_on", "set_brightness"):
            if action == "set_brightness" or params.get("brightness") is not None:
                pct = _number(params.get("brightness"), "brightness")
                if not 0 <= pct <= 100:
                    raise ControlRequestError("brightness out of range")
                if pct == 0:
                    return ServiceCall("light", "turn_off", target)
                if dimmable:
                    return ServiceCall(
                        "light",
                        "turn_on",
                        {**target, "brightness": max(1, pct_to_ha(pct))},
                    )
                if action == "set_brightness":
                    raise ControlRequestError("light is not dimmable")
            return ServiceCall("light", "turn_on", target)

    elif kind == "switch":
        if action in ("turn_on", "turn_off"):
            return ServiceCall("switch", action, target)

    elif kind == "cover":
        if action == "set_position":
            position = _number(params.get("position"), "position")
            if not 0 <= position <= 100:
                raise ControlRequestError("position out of range")
            features = _features(attributes)
            if features & COVER_SET_POSITION:
                return ServiceCall(
                    "cover",
                    "set_cover_position",
                    {**target, "position": round(position)},
                )
            # Open/close-only cover: the nearest end.
            if position >= 50 and features & COVER_OPEN:
                return ServiceCall("cover", "open_cover", target)
            if position < 50 and features & COVER_CLOSE:
                return ServiceCall("cover", "close_cover", target)
            raise ControlRequestError("cover cannot move there")

    elif kind == "scene" and action == "activate":
        return ServiceCall("scene", "turn_on", target)

    raise ControlRequestError(f"action {action!r} is not valid for {kind}")
