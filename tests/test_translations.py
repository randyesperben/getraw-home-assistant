"""Contract 6: every event type and entity has its name in both languages.

A name missing from one file shows the raw key in that language's Home
Assistant, and Spanish installs build their entity ids from the Spanish names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.getraw.const import EVENT_TYPES, PHONE_EVENT_TYPES

from .conftest import CONTRACTS

_TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components" / "getraw" / "translations"
)


def _keys(node: Any, prefix: str = "") -> set[str]:
    if not isinstance(node, dict):
        return {prefix}
    return set().union(*(_keys(v, f"{prefix}.{k}") for k, v in node.items()))


def _load(language: str) -> dict[str, Any]:
    return json.loads((_TRANSLATIONS / f"{language}.json").read_text("utf-8"))


def test_english_and_spanish_name_the_same_things() -> None:
    english, spanish = _keys(_load("en")), _keys(_load("es"))
    assert english, "no English names"
    assert english == spanish


def test_every_event_type_is_named() -> None:
    named = _load("en")["entity"]["event"]["events"]["state_attributes"][
        "event_type"
    ]["state"]
    assert sorted(named) == sorted(EVENT_TYPES)


def test_home_assistant_reads_the_event_types_the_phone_writes() -> None:
    """A type only one side names is an event no automation sees."""
    schema = json.loads((CONTRACTS / "ha_event.schema.json").read_text("utf-8"))
    written = {branch["properties"]["type"]["const"] for branch in schema["$defs"]["event"]["oneOf"]}
    assert written, "no event types in ha_event.schema.json"
    assert written == set(PHONE_EVENT_TYPES)


# Each state Home Assistant names for a value the phone writes, and where the contract lists
# that value, with the states Home Assistant adds itself: (platform, entity, attribute or None)
# → (schema, JSON pointer to its enum, Home Assistant's own states).
_CONTRACT_STATES = {
    ("sensor", "phase", None): (
        "ha_state.schema.json",
        "/$defs/workout/properties/phase",
        {"idle"},  # no workout: the phone writes no phase then
    ),
    ("sensor", "current_set", "set_type"): ("ha_state.schema.json", "/$defs/set_type", set()),
    ("sensor", "latest_record", "kind"): ("ha_state.schema.json", "/$defs/record_kind", set()),
    ("sensor", "todays_plan", None): ("ha_summary.schema.json", "/$defs/day/properties/plan", set()),
    ("sensor", "todays_plan", "reason"): (
        "ha_summary.schema.json",
        "/$defs/day/properties/reason",
        set(),
    ),
    ("sensor", "last_workout_feeling", None): (
        "ha_summary.schema.json",
        "/properties/health/properties/effort/properties/last_workout/properties/feeling",
        set(),
    ),
}


def _pointer(document: dict[str, Any], pointer: str) -> Any:
    node: Any = document
    for part in pointer.strip("/").split("/"):
        node = node[part]
    return node


def test_every_value_the_phone_writes_has_its_name() -> None:
    """A new record kind, set type or plan reason reaches the schema first: Home Assistant
    would show its raw key in both languages."""
    entities = _load("en")["entity"]
    for (platform, key, attribute), (schema, pointer, own) in _CONTRACT_STATES.items():
        entity = entities[platform][key]
        named = (
            entity["state"]
            if attribute is None
            else entity["state_attributes"][attribute]["state"]
        )
        document = json.loads((CONTRACTS / schema).read_text("utf-8"))
        written = set(_pointer(document, pointer)["enum"])
        assert written, f"{schema}{pointer}: no enum"
        assert set(named) == written | own, (key, attribute)
