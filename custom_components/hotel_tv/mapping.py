"""Fill-by-pattern: copy one TV's exposed entities to the other TVs.

Pure helpers (no Home Assistant imports), the same idea as in the Tape Chart
integration: the entity ids of the rooms of a hotel differ only in the room
number (`climate.room_203_thermostat` → `climate.room_204_thermostat`), and the
TVs are named after their room ("TV 203", "203"), so the number in the TV's name
finds the entities of the TV's room.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import re

from .const import CONTROL_DOMAINS


def number_slug(number: str) -> str:
    """A room name as it appears inside entity ids ("A-1" -> "a_1")."""
    return re.sub(r"[^0-9a-z]+", "_", number.strip().lower()).strip("_")


def _number_pattern(number: str) -> re.Pattern[str]:
    """Match a room number as a whole token inside an entity id."""
    token = re.escape(number)
    if number.isdigit():
        # room_205_pir, room205pir: bounded by non-digits only.
        return re.compile(rf"(?<![0-9]){token}(?![0-9])")
    return re.compile(rf"(?<![0-9a-z]){token}(?![0-9a-z])")


def substitute_number(entity_id: str, source: str, target: str) -> str | None:
    """Swap the source room number for the target's; None if it is not there."""
    src, dst = number_slug(source), number_slug(target)
    if not src or not dst or src == dst:
        return None
    domain, sep, object_id = entity_id.partition(".")
    if not sep:
        return None
    new_object_id, count = _number_pattern(src).subn(dst, object_id)
    if count == 0:
        return None
    return f"{domain}.{new_object_id}"


def _digits(name: str) -> str | None:
    """The number in a name like "Room 203" / "TV 203" (exactly one number)."""
    found = re.findall(r"[0-9]+", name)
    return found[0] if len(found) == 1 else None


def substitute_room(entity_id: str, source: str, target: str) -> str | None:
    """The entity id with the source room's name replaced by the target's.

    First the whole name ("203", "A-1" as `a_1`), then — for names like
    "Room 203" or "TV 203" — the number alone. None when the entity id carries
    neither.
    """
    candidate = substitute_number(entity_id, source, target)
    if candidate is not None:
        return candidate
    src, dst = _digits(source), _digits(target)
    if src is None or dst is None:
        return None
    return substitute_number(entity_id, src, dst)


@dataclass(frozen=True, slots=True)
class Target:
    """A TV to fill: its name and the entities it already exposes."""

    name: str
    entities: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Proposal:
    """What fill-by-pattern found for one TV."""

    # {entity_id: label override}, in the source TV's order.
    entities: dict[str, str] = field(default_factory=dict)
    # Entity ids that were expected but do not exist (the source's own id when
    # it does not carry the room name).
    missing: list[str] = field(default_factory=list)


def fill_by_pattern(
    source_name: str,
    source_entities: Mapping[str, str],
    targets: Mapping[str, Target],
    exists: Callable[[str], bool],
    overwrite: bool = False,
) -> dict[str, Proposal]:
    """Propose the exposed entities of other TVs from one TV's.

    Per source entity: its id with the source name swapped for the target's, when
    such an entity exists (and is of a domain that can be exposed); otherwise it
    is reported missing. The source's label override is carried over. A TV that
    already exposes something is skipped unless `overwrite` (its list is then
    replaced by what was found). Every other TV is returned, also when nothing
    was found for it (`entities` empty) — such a TV is never changed.
    """
    result: dict[str, Proposal] = {}
    for target_id, target in targets.items():
        if target.entities and not overwrite:
            continue
        proposal = Proposal()
        for entity_id, label in source_entities.items():
            candidate = substitute_room(entity_id, source_name, target.name)
            if candidate is None:
                proposal.missing.append(entity_id)
            elif (
                candidate.partition(".")[0] not in CONTROL_DOMAINS
                or candidate in source_entities
                or not exists(candidate)
            ):
                proposal.missing.append(candidate)
            else:
                proposal.entities[candidate] = str(label or "")
        result[target_id] = proposal
    return result
