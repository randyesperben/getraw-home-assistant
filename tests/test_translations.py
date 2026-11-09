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
    """AUDIT-L2-1 M14: a type only one side names is an event no automation sees."""
    schema = json.loads((CONTRACTS / "ha_event.schema.json").read_text("utf-8"))
    written = {branch["properties"]["type"]["const"] for branch in schema["$defs"]["event"]["oneOf"]}
    assert written, "no event types in ha_event.schema.json"
    assert written == set(PHONE_EVENT_TYPES)
