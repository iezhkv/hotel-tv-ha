"""Fill-by-pattern helpers (pure functions)."""

from __future__ import annotations

import pytest

from custom_components.hotel_tv.mapping import (
    Proposal,
    Target,
    fill_by_pattern,
    number_slug,
    substitute_number,
    substitute_room,
)

THERMOSTAT_203 = "climate.room_203_thermostat_climate_controller_2"


@pytest.mark.parametrize(
    ("name", "slug"),
    [("203", "203"), (" A-1 ", "a_1"), ("Suite 12/B", "suite_12_b"), ("—", "")],
)
def test_number_slug(name: str, slug: str) -> None:
    """A room name the way entity ids spell it."""
    assert number_slug(name) == slug


@pytest.mark.parametrize(
    ("entity_id", "source", "target", "expected"),
    [
        (
            THERMOSTAT_203,
            "203",
            "204",
            "climate.room_204_thermostat_climate_controller_2",
        ),
        ("light.room203_main", "203", "1204", "light.room1204_main"),
        ("light.r203_203_bed", "203", "101", "light.r101_101_bed"),
        # The number only as a whole number: 2030 and 1203 are other rooms.
        ("light.room_2030_main", "203", "204", None),
        ("light.room_1203_main", "203", "204", None),
        # The domain is never touched.
        ("switch.fan", "switch", "light", None),
        ("light.lobby", "203", "204", None),
        ("light.room_a_1_main", "A-1", "B-2", "light.room_b_2_main"),
        ("light.room_la_1_main", "A-1", "B-2", None),
        ("light.room_203_main", "203", "203", None),
        ("light.room_203_main", "", "204", None),
        ("not_an_entity_id", "203", "204", None),
    ],
)
def test_substitute_number(
    entity_id: str, source: str, target: str, expected: str | None
) -> None:
    """The room number is swapped inside the object id, as a whole token."""
    assert substitute_number(entity_id, source, target) == expected


def test_substitute_room_named_rooms() -> None:
    """Rooms called "Room 203": the whole name first, then the number alone."""
    assert (
        substitute_room("light.room_203_main", "Room 203", "Room 204")
        == "light.room_204_main"
    )
    assert (
        substitute_room("light.r203_main", "Стая 203", "Стая 204") == "light.r204_main"
    )
    # No single number to go by.
    assert (
        substitute_room("light.r203_main", "Wing 2 Room 203", "Wing 2 Room 204") is None
    )
    assert substitute_room("light.lobby", "Room 203", "Room 204") is None


def _fill(existing: set[str], targets: dict[str, Target], **kwargs) -> dict:
    return fill_by_pattern(
        "203",
        {
            THERMOSTAT_203: "Климатик",
            "light.room_203_main": "",
            "scene.night": "Нощ",
        },
        targets,
        existing.__contains__,
        **kwargs,
    )


def test_fill_by_pattern() -> None:
    """Found entities keep the source's order and labels; the rest is missing."""
    existing = {
        "climate.room_204_thermostat_climate_controller_2",
        "light.room_204_main",
        "climate.room_205_thermostat_climate_controller_2",
    }
    result = _fill(
        existing,
        {
            "r204": Target("204"),
            "r205": Target("205"),
            "r206": Target("206"),
        },
    )
    assert result == {
        "r204": Proposal(
            entities={
                "climate.room_204_thermostat_climate_controller_2": "Климатик",
                "light.room_204_main": "",
            },
            # The scene has no room number in its id: it cannot be copied.
            missing=["scene.night"],
        ),
        "r205": Proposal(
            entities={"climate.room_205_thermostat_climate_controller_2": "Климатик"},
            missing=["light.room_205_main", "scene.night"],
        ),
        "r206": Proposal(
            entities={},
            missing=[
                "climate.room_206_thermostat_climate_controller_2",
                "light.room_206_main",
                "scene.night",
            ],
        ),
    }
    assert list(result["r204"].entities) == [
        "climate.room_204_thermostat_climate_controller_2",
        "light.room_204_main",
    ]


def test_fill_by_pattern_overwrite() -> None:
    """A room that already has entities is touched only with overwrite."""
    existing = {"light.room_204_main"}
    targets = {
        "r204": Target("204", {"switch.room_204_fan": "Вентилатор"}),
        "r205": Target("205"),
    }
    assert set(_fill(existing, targets)) == {"r205"}
    result = _fill(existing, targets, overwrite=True)
    assert set(result) == {"r204", "r205"}
    assert result["r204"].entities == {"light.room_204_main": ""}


def test_fill_by_pattern_never_copies_the_source_or_other_domains() -> None:
    """A swap that lands on one of the source's own entities is not a match."""
    result = fill_by_pattern(
        "1",
        {"light.room_1_main": "", "light.room_2_main": ""},
        {"r2": Target("2")},
        lambda entity_id: True,
    )
    # light.room_1_main → light.room_2_main is the source's own second entity.
    assert result["r2"].entities == {}
    assert result["r2"].missing == ["light.room_2_main", "light.room_2_main"]
    # A domain that cannot be a control is not proposed even if it exists.
    result = fill_by_pattern(
        "203", {"vacuum.room_203": ""}, {"r204": Target("204")}, lambda entity_id: True
    )
    assert result["r204"] == Proposal(entities={}, missing=["vacuum.room_204"])
