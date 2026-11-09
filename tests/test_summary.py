"""The history summary's entities, its day turn, its units and the calendar (contract 6).

The summary fixtures are Monday 9 November 2026 in Europe/Madrid, weeks from Monday.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any

from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from custom_components.getraw.const import DOMAIN

from .phone import ALL_CATEGORIES, Phone, entity_state, summary_fixture, workout
from .test_pairing import pair

MADRID = dt_util.get_time_zone("Europe/Madrid")
MONDAY_MORNING = datetime(2026, 11, 9, 11, 0, tzinfo=MADRID)


async def _paired_with(
    hass: HomeAssistant, client: TestClient, name: str, **changes: Any
) -> Phone:
    phone, _ = await pair(hass, client)
    await phone.post(phone.body("summary", **(summary_fixture(name) | changes)))
    await hass.async_block_till_done()
    return phone


def _value(hass: HomeAssistant, key: str, platform: str = "sensor") -> str:
    return entity_state(hass, platform, key).state


async def test_todays_figures_come_from_todays_entry(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MONDAY_MORNING)
    await _paired_with(hass, raw_client, "typical")

    plan = entity_state(hass, "sensor", "todays_plan")
    assert (plan.state, plan.attributes["reason"]) == ("due", "scheduled_today")
    assert _value(hass, "next_routine") == "Pierna A"
    assert _value(hass, "next_routine_date") == "2026-11-09"
    assert _value(hass, "trained_today", "binary_sensor") == "off"
    assert _value(hass, "weekly_goal") == "3"
    assert _value(hass, "weekly_goal_met", "binary_sensor") == "off"
    assert _value(hass, "weeks_in_a_row") == "6"
    week = entity_state(hass, "sensor", "workouts_this_week")
    assert week.state == "0"
    assert week.attributes["last_reset"] == "2026-11-09T00:00:00+01:00"
    month = entity_state(hass, "sensor", "workouts_this_month")
    assert month.state == "5"
    assert month.attributes["last_reset"] == "2026-11-01T00:00:00+01:00"
    assert _value(hass, "records_this_month") == "3"
    assert _value(hass, "workouts_all_time") == "214"
    last = entity_state(hass, "sensor", "last_workout")
    assert last.state == "2026-11-07T10:14:40+00:00"
    assert (last.attributes["title"], last.attributes["sets"]) == ("Tirón", 21)
    assert last.attributes["notes"].startswith("Hombro derecho")
    assert float(_value(hass, "last_workout_duration")) == pytest.approx(4349 / 60)
    assert _value(hass, "last_workout_feeling") == "good"


async def test_the_day_turns_at_the_phones_midnight_and_the_month_starts_at_zero(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MONDAY_MORNING)
    await _paired_with(hass, raw_client, "typical")

    freezer.move_to(datetime(2026, 11, 10, 0, 0, 1, tzinfo=MADRID))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    plan = entity_state(hass, "sensor", "todays_plan")
    assert (plan.state, plan.attributes["reason"]) == ("rest", "next_scheduled")
    assert _value(hass, "next_routine") == "Empuje"

    # Past the fourteen days the phone sent: unknown, never a stale day.
    freezer.move_to(datetime(2026, 11, 23, 0, 0, 1, tzinfo=MADRID))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _value(hass, "todays_plan") == STATE_UNKNOWN
    assert _value(hass, "workouts_this_week") == STATE_UNKNOWN

    freezer.move_to(datetime(2026, 12, 1, 0, 0, 1, tzinfo=MADRID))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    month = entity_state(hass, "sensor", "workouts_this_month")
    assert month.state == "0"
    assert month.attributes["last_reset"] == "2026-12-01T00:00:00+01:00"


async def test_a_week_from_sunday_starts_again_on_sunday(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    """AUDIT-L2-3 M4: the weekly totals restart on the person's first day of the week."""
    freezer.move_to(datetime(2026, 11, 14, 11, 0, tzinfo=MADRID))  # a Saturday
    await _paired_with(hass, raw_client, "typical", first_day_of_week=7)
    week = entity_state(hass, "sensor", "workouts_this_week")
    assert week.attributes["last_reset"] == "2026-11-08T00:00:00+01:00"


async def test_a_new_zone_or_a_restart_never_starts_the_month_again(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    """AUDIT-L2-3 M5: the recorder counts the month again at any other last_reset."""
    freezer.move_to(MONDAY_MORNING)
    phone = await _paired_with(hass, raw_client, "typical")
    (entry,) = hass.config_entries.async_entries(DOMAIN)

    def last_reset() -> str:
        return entity_state(hass, "sensor", "workouts_this_month").attributes["last_reset"]

    assert last_reset() == "2026-11-01T00:00:00+01:00"
    travelling = summary_fixture("typical") | {"time_zone": "America/New_York"}
    await phone.post(phone.body("summary", **travelling))
    assert last_reset() == "2026-11-01T00:00:00+01:00"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert last_reset() == "2026-11-01T00:00:00+01:00"


async def test_the_recorder_keeps_no_notes_or_titles(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    """AUDIT-L2-3 M3: the states table would keep them after their switch goes off."""
    freezer.move_to(MONDAY_MORNING)
    phone, _ = await pair(hass, raw_client)
    written: list[Event[EventStateChangedData]] = []
    last_workout = entity_state(hass, "sensor", "last_workout").entity_id

    @callback
    def keep(event: Event[EventStateChangedData]) -> None:
        written.append(event)

    async_track_state_change_event(hass, [last_workout], keep)
    await phone.post(phone.body("summary", **summary_fixture("typical")))
    await hass.async_block_till_done()

    shown = written[-1].data["new_state"].attributes
    assert shown["title"] and shown["notes"]
    recorded = json.loads(StateAttributes.shared_attrs_bytes_from_event(written[-1], None))
    assert "title" not in recorded and "notes" not in recorded
    assert recorded["sets"] == shown["sets"]


async def test_weights_and_lengths_are_suggested_in_the_persons_units(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    """S-I1: Home Assistant reads a suggested unit only when the entity is first made."""
    freezer.move_to(MONDAY_MORNING)
    phone, _ = await pair(hass, raw_client)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", "getraw", f"{phone.account_ref}_body_weight") is None

    await phone.post(
        phone.body("summary", **(summary_fixture("typical") | {"weight_unit": "lb", "length_unit": "in"}))
    )
    await hass.async_block_till_done()
    weight = entity_state(hass, "sensor", "body_weight")
    assert weight.attributes["unit_of_measurement"] == "lb"
    assert float(weight.state) == pytest.approx(81.4 / 0.45359237)
    waist = registry.async_get(
        registry.async_get_entity_id("sensor", "getraw", f"{phone.account_ref}_waist")
    )
    assert waist.options["sensor.private"]["suggested_unit_of_measurement"] == "in"


async def test_categories_off_leave_their_entities_unavailable(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MONDAY_MORNING)
    phone, _ = await pair(hass, raw_client)
    # The names left in the body on purpose: Home Assistant shows none with Titles off.
    body = summary_fixture("done_today_titles_off")
    body["health"]["titles"] = summary_fixture("typical")["health"]["titles"]
    await phone.post(phone.body("summary", **body))
    await hass.async_block_till_done()

    for key in ("next_routine", "body_weight", "last_workout_volume", "volume_this_month"):
        assert _value(hass, key) == STATE_UNAVAILABLE, key
    attributes = entity_state(hass, "sensor", "last_workout").attributes
    assert "title" not in attributes
    assert "notes" not in attributes
    assert _value(hass, "last_workout_effort") != STATE_UNAVAILABLE
    assert _value(hass, "todays_plan") == "done"


async def test_the_calendar_shows_past_workouts_and_planned_days(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MONDAY_MORNING)
    phone = await _paired_with(hass, raw_client, "typical")
    calendar = entity_state(hass, "calendar", "workouts").entity_id

    async def events() -> list[tuple[str, str]]:
        response = await hass.services.async_call(
            "calendar",
            "get_events",
            {
                "entity_id": calendar,
                "start_date_time": "2026-11-01T00:00:00+01:00",
                "end_date_time": "2026-11-15T00:00:00+01:00",
            },
            blocking=True,
            return_response=True,
        )
        return [(e["start"], e["summary"]) for e in response[calendar]["events"]]

    listed = await events()
    assert ("2026-11-07T09:02:11+00:00", "Tirón") in listed
    assert ("2026-11-09", "Pierna A") in listed
    assert ("2026-11-11", "Empuje") in listed
    assert not any(start == "2026-11-10" for start, _ in listed)  # a rest day

    # Titles off: the phone sends no names, the events take Home Assistant's words.
    # The names left in the body on purpose: Home Assistant shows none with Titles off.
    untitled = summary_fixture("typical") | {"categories": ["effort"]}
    await phone.post(phone.body("summary", **untitled))
    assert {summary for _, summary in await events()} == {"Workout", "Planned workout"}


async def test_a_switch_turned_off_hides_the_summarys_figures_at_once(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    """AUDIT-L2-3 I2: the next state says it, before the next summary comes."""
    freezer.move_to(MONDAY_MORNING)
    phone = await _paired_with(hass, raw_client, "typical")
    assert _value(hass, "body_weight") == "81.4"

    # Saved volume still on: it counts the body weight, so it hides with it.
    on = [c for c in ALL_CATEGORIES if c != "body_weight"]
    await phone.post(phone.body("state", live={"phase": "idle"}, categories=on))
    for key in ("body_weight", "last_workout_volume", "volume_this_month"):
        assert _value(hass, key) == STATE_UNAVAILABLE, key
    assert _value(hass, "body_fat_pct") == "17.8"


async def test_health_a_body_does_not_share_never_reaches_the_disk(
    hass: HomeAssistant,
    raw_client: TestClient,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """AUDIT-L2-3 I1: the store goes into backups, so it keeps only the health the phone shares."""
    freezer.move_to(MONDAY_MORNING)
    phone, entry = await pair(hass, raw_client)
    # Every value in both bodies; Saved volume on without Body weight shares nothing either.
    await phone.post(phone.body("state", live=workout(), categories=["saved_volume"]))
    summary = summary_fixture("typical") | {"categories": ["saved_volume"]}
    await phone.post(phone.body("summary", **summary))
    freezer.tick(timedelta(seconds=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    view = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["view"]
    assert (view["live"]["phase"], view["summary"]["time_zone"]) == ("active", "Europe/Madrid")
    assert "health" not in view["live"]
    assert "health" not in view["summary"]


def _leaves(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in _leaves(value)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in _leaves(value)]
    return [node]


def _shows(shown: Any, value: Any) -> bool:
    """Whether a state or attribute shows ``value``; a number may be shown as text."""
    if isinstance(value, str) or isinstance(shown, bool):
        return shown == value
    try:
        return float(shown) == float(value)
    except (TypeError, ValueError):
        return False


async def _everything_shown(hass: HomeAssistant, entry: ConfigEntry) -> list[Any]:
    """Every state, attribute and calendar event of the device, enum options aside."""
    shown: list[Any] = []
    for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if (state := hass.states.get(entity.entity_id)) is not None:
            attributes = {k: v for k, v in state.attributes.items() if k != "options"}
            shown += [state.state, *_leaves(attributes)]
    calendar = entity_state(hass, "calendar", "workouts").entity_id
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": calendar,
            "start_date_time": "2026-10-01T00:00:00+02:00",
            "end_date_time": "2026-12-01T00:00:00+01:00",
        },
        blocking=True,
        return_response=True,
    )
    return shown + _leaves(response[calendar]["events"])


async def test_every_health_value_shows_only_while_its_switch_is_on(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    """AUDIT-L2-3 I3: every value is first seen somewhere, so nothing passes by absence."""
    freezer.move_to(MONDAY_MORNING)
    phone, entry = await pair(hass, raw_client)
    typical = summary_fixture("typical")
    assert set(typical["health"]) <= set(typical["categories"])  # every switch on
    health = _leaves(typical["health"])
    await phone.post(phone.body("summary", **typical))
    await hass.async_block_till_done()
    # The entities off by default turned on, as a person does; HA reloads the entry 30 s later.
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.disabled_by is not None:
            registry.async_update_entity(entity.entity_id, disabled_by=None)
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    shown = await _everything_shown(hass, entry)
    assert [value for value in health if not any(_shows(s, value) for s in shown)] == []

    # Every switch off in the next state: the summary HA keeps still holds the
    # values (a summary would drop them before they are kept), so each entity's
    # own category check is what hides them.
    await phone.post(phone.body("state", live={"phase": "idle"}, categories=[]))
    await hass.async_block_till_done()
    shown = await _everything_shown(hass, entry)
    assert [value for value in health if any(_shows(s, value) for s in shown)] == []
