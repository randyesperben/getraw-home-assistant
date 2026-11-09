"""The live workout's entities, the event entity and Home Assistant's own timers (contract 6)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event as HassEvent, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from custom_components.getraw import event as event_platform
from custom_components.getraw.const import DOMAIN

from .phone import (
    ACCOUNT,
    ALL_CATEGORIES,
    EXERCISE,
    STARTED,
    Phone,
    entity_state,
    event_of,
    instant,
    resting,
    workout,
)
from .test_pairing import pair


def _state(phone: Phone, live: dict[str, Any], categories: list[str] = ALL_CATEGORIES) -> dict:
    return phone.body("state", live=live, categories=categories)


def _fired(hass: HomeAssistant) -> list[str]:
    """The event types the event entity fires, as ``trigger: event.received`` sees them."""
    fired: list[str] = []
    entity_id = er.async_get(hass).async_get_entity_id("event", DOMAIN, f"{ACCOUNT}_events")
    assert entity_id is not None

    @callback
    def collect(event: HassEvent[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        # A change from unavailable is the state restored at setup, which
        # event.received ignores too; one to unavailable is the unload.
        if old is None or new is None or old.state == new.state:
            return
        if STATE_UNAVAILABLE in (old.state, new.state):
            return
        fired.append(new.attributes["event_type"])

    async_track_state_change_event(hass, [entity_id], collect)
    return fired


def _value(hass: HomeAssistant, platform: str, key: str) -> str:
    return entity_state(hass, platform, key).state


async def test_the_live_workout_reaches_its_entities_and_idle_clears_them(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, _ = await pair(hass, raw_client)
    await phone.post(_state(phone, workout()))

    assert _value(hass, "binary_sensor", "workout") == "on"
    assert _value(hass, "sensor", "phase") == "active"
    assert _value(hass, "sensor", "workout_title") == "Push A"
    assert _value(hass, "sensor", "workout_started") == STARTED.isoformat()
    exercise = entity_state(hass, "sensor", "current_exercise")
    assert (exercise.state, exercise.attributes["index"], exercise.attributes["total"]) == (
        "Bench Press",
        1,
        3,
    )
    current_set = entity_state(hass, "sensor", "current_set")
    assert current_set.state == "3"
    assert current_set.attributes["target"] == "80 kg × 8–10"
    assert current_set.attributes["target_rpe"] == 8
    sets_done = entity_state(hass, "sensor", "sets_done")
    assert (sets_done.state, sets_done.attributes["total"]) == ("2", 9)
    assert _value(hass, "sensor", "rest_end") == STATE_UNKNOWN

    await phone.post(_state(phone, {"phase": "idle"}))
    assert _value(hass, "binary_sensor", "workout") == "off"
    assert _value(hass, "sensor", "phase") == "idle"
    for key in ("current_exercise", "current_set", "workout_title", "workout_started"):
        assert _value(hass, "sensor", key) == STATE_UNKNOWN, key


async def test_a_health_category_turned_off_shows_nothing_of_it(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, _ = await pair(hass, raw_client)
    await phone.post(_state(phone, workout(), categories=["measurements"]))

    assert _value(hass, "sensor", "workout_title") == STATE_UNAVAILABLE
    attributes = entity_state(hass, "sensor", "current_set").attributes
    assert "target_rpe" not in attributes
    assert "previous_rpe" not in attributes


async def test_the_event_entity_never_shows_health_across_a_switch_and_a_restart(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """AUDIT-L2-3 C1: HA keeps the last event's attributes and restores them, so none is health."""
    phone, entry = await pair(hass, raw_client)
    # Built here: the contract's fixtures no longer carry health in events, and a
    # phone that still sends it must not show it either.
    finished = event_of("workout_finished", dt_util.utcnow()) | {
        "data": {"duration_sec": 5300, "exercises": 3, "records": 2, "sets": 9},
        "health": {
            "effort": {"feeling": "good", "session_rpe": 8},
            "notes": {"notes": "Shoulder felt fine."},
            "titles": {"title": "Push A"},
        },
    }
    # Effort, Titles and Notes in the event, and the state after it with its own health.
    health = {key for fields in finished["health"].values() for key in fields}
    health |= {key for fields in workout()["health"].values() for key in fields}
    await phone.post(phone.body("event", events=[finished], live=workout(), categories=ALL_CATEGORIES))

    def shown() -> dict[str, Any]:
        attributes = dict(entity_state(hass, "event", "events").attributes)
        assert attributes["event_type"] == "workout_finished"
        assert attributes["sets"] == 9  # its data: the event is there
        return attributes

    assert health.isdisjoint(shown())
    await phone.post(_state(phone, {"phase": "idle"}, categories=["measurements"]))
    assert health.isdisjoint(shown())
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert health.isdisjoint(shown())


async def test_events_fire_in_order_with_their_data_and_text(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, _ = await pair(hass, raw_client)
    received: list[tuple[str, dict[str, Any]]] = []
    entity_id = entity_state(hass, "event", "events").entity_id

    @callback
    def collect(event: HassEvent[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if state is not None and state.attributes.get("event_type"):
            # What an automation reads at that moment: the state the events left.
            resting_now = entity_state(hass, "binary_sensor", "resting").state
            received.append(
                (state.attributes["event_type"], dict(state.attributes) | {"_resting": resting_now})
            )

    async_track_state_change_event(hass, [entity_id], collect)
    now = dt_util.utcnow()
    done = event_of("set_completed", now)
    done["data"] = {"exercise_name": "Bench Press", "values": {"reps": 8, "weight_kg": 82.5}}
    done["text"] = "Bench Press, set 2 of 3: 82.5 kg × 8"
    record = event_of("record", now)
    record["data"] = {"records": [{"kind": "max_weight", "value": 82.5}]}
    newer = event_of("something_new", now)  # a newer phone's type: ignored
    forged = event_of("rest_ended", now)  # only Home Assistant fires this one
    rest = event_of("rest_started", now)
    await phone.post(
        phone.body(
            "event",
            events=[done, record, newer, forged, rest],
            live=resting(120),
            categories=ALL_CATEGORIES,
        )
    )
    await hass.async_block_till_done()

    assert [kind for kind, _ in received] == ["set_completed", "record", "rest_started"]
    attributes = received[0][1]
    assert attributes["values"] == {"reps": 8, "weight_kg": 82.5}
    assert attributes["text"] == "Bench Press, set 2 of 3: 82.5 kg × 8"
    assert received[1][1]["records"] == [{"kind": "max_weight", "value": 82.5}]
    assert received[2][1]["_resting"] == "on"


async def test_event_times_are_on_home_assistants_clock_as_the_sensors_are(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """AUDIT-L2-3 M6: an automation on a rest's ends_at and one on Rest end agree."""
    phone, _ = await pair(hass, raw_client, behind=timedelta(minutes=10))
    phone_clock = dt_util.utcnow() - phone.behind
    started, ends = instant(phone_clock), instant(phone_clock + timedelta(seconds=120))
    rest = event_of("rest_started", phone_clock)
    rest["data"] = {"rest_started_at": started, "ends_at": ends, "seconds": 120}
    live = workout(phase="resting", rest_started_at=started, rest_ends_at=ends)
    await phone.post(phone.body("event", events=[rest], live=live, categories=ALL_CATEGORIES))

    shown = entity_state(hass, "event", "events").attributes
    assert (shown["event_type"], shown["seconds"]) == ("rest_started", 120)
    rest_end = dt_util.parse_datetime(_value(hass, "sensor", "rest_end"))
    ends_at = dt_util.parse_datetime(shown["ends_at"])
    started_at = dt_util.parse_datetime(shown["rest_started_at"])
    assert rest_end is not None and ends_at is not None and started_at is not None
    assert abs(ends_at - rest_end) < timedelta(seconds=1)
    assert abs(started_at - dt_util.utcnow()) < timedelta(seconds=1)


async def test_a_lock_screen_patch_keeps_only_what_still_describes_the_set(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    """AUDIT-L1-1 I10: a tap that moves the set or exercise leaves no old name or target."""
    phone, _ = await pair(hass, raw_client)
    await phone.post(_state(phone, workout()))

    def patch(exercise: dict[str, Any], set_: dict[str, Any]) -> dict[str, Any]:
        now = dt_util.utcnow()
        return phone.body(
            "patch",
            events=[event_of("set_completed", now)],
            live={
                "started_at": instant(STARTED),
                "phase": "active",
                "completed_sets": 3,
                "exercise": exercise,
                "set": set_,
            },
        )

    next_set = {"id": "next-set", "index": 1, "total": 3, "set_type": "normal"}
    await phone.post(patch({"id": EXERCISE, "index": 1, "total": 3}, next_set))
    assert _value(hass, "sensor", "current_exercise") == "Bench Press"
    current_set = entity_state(hass, "sensor", "current_set")
    assert current_set.state == "1"
    assert "target" not in current_set.attributes
    assert "target_rpe" not in current_set.attributes
    assert _value(hass, "sensor", "sets_done") == "3"

    # Two hours of lock-screen sets with RAW closed: each tap moves the abandon time.
    freezer.tick(timedelta(hours=2))
    await phone.post(patch({"id": "next-exercise", "index": 2, "total": 3}, next_set | {"id": "s2"}))
    exercise = entity_state(hass, "sensor", "current_exercise")
    assert (exercise.state, exercise.attributes["index"]) == (STATE_UNKNOWN, 2)
    freezer.tick(timedelta(hours=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _value(hass, "binary_sensor", "workout") == "on"


async def test_home_assistant_rings_the_rest_end_once_and_never_after_a_skip(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    phone, entry = await pair(hass, raw_client)
    fired = _fired(hass)
    await phone.post(_state(phone, resting(30)))
    assert _value(hass, "binary_sensor", "resting") == "on"

    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert fired == ["rest_ended"]
    assert _value(hass, "binary_sensor", "resting") == "off"
    assert _value(hass, "sensor", "phase") == "active"
    assert _value(hass, "sensor", "rest_end") == STATE_UNKNOWN
    # The phone's own state for that end, a second later, rings nothing more.
    freezer.tick(timedelta(seconds=1))
    await phone.post(_state(phone, workout()))
    assert fired == ["rest_ended"]

    # Skipped from the lock screen: the patch says so, nothing rings.
    await phone.post(_state(phone, resting(60)))
    freezer.tick(timedelta(seconds=10))
    skip = event_of("rest_skipped", dt_util.utcnow())
    await phone.post(
        phone.body("patch", events=[skip], live={"started_at": instant(STARTED), "phase": "active"})
    )
    freezer.tick(timedelta(seconds=60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert fired == ["rest_ended", "rest_skipped"]

    # The phone's own timer ran out a moment before Home Assistant's: it rings now, once.
    await phone.post(_state(phone, resting(20)))
    freezer.tick(timedelta(seconds=18))
    await phone.post(_state(phone, workout()))
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert fired == ["rest_ended", "rest_skipped", "rest_ended"]


async def test_an_abandoned_workout_turns_off_once_and_stays_off(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    phone, entry = await pair(hass, raw_client)
    fired = _fired(hass)
    left = workout()
    await phone.post(_state(phone, left))

    freezer.tick(timedelta(hours=3, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert fired == ["workout_abandoned"]
    assert _value(hass, "binary_sensor", "workout") == "off"
    assert _value(hass, "sensor", "phase") == "idle"

    # RAW opened the next morning: the same workout, no new activity.
    freezer.tick(timedelta(hours=9))
    await phone.post(_state(phone, left))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _value(hass, "binary_sensor", "workout") == "off"
    assert fired == ["workout_abandoned"]


async def test_a_restart_shows_the_workout_again_and_rings_only_on_time(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    phone, entry = await pair(hass, raw_client)
    await phone.post(_state(phone, resting(60)))

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    fired = _fired(hass)
    assert _value(hass, "binary_sensor", "resting") == "on"
    assert _value(hass, "sensor", "current_exercise") == "Bench Press"
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert fired == ["rest_ended"]

    # Home Assistant down across a rest's end: it does not ring late.
    await phone.post(_state(phone, resting(5)))
    assert await hass.config_entries.async_unload(entry.entry_id)
    freezer.tick(timedelta(seconds=30))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fired = _fired(hass)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert fired == []
    assert _value(hass, "binary_sensor", "resting") == "off"

    # A timer that runs late (the host slept through the rest's end) rings nothing.
    await phone.post(_state(phone, resting(30)))
    freezer.tick(timedelta(seconds=60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert fired == []


async def test_a_rest_end_due_while_home_assistant_starts_still_rings(
    hass: HomeAssistant,
    raw_client: TestClient,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUDIT-L2-3 C2: a cold start loads the platforms in the executor; the timers wait for them."""
    phone, entry = await pair(hass, raw_client)
    await phone.post(_state(phone, resting(30)))
    assert await hass.config_entries.async_unload(entry.entry_id)
    fired = _fired(hass)  # an automation listens before Home Assistant starts
    freezer.tick(timedelta(seconds=32))  # 2 s past the rest's end, inside its 10 s

    set_up = event_platform.async_setup_entry

    async def cold_setup(*args: Any) -> None:
        for _ in range(3):  # the imports and translations a cold start reads from disk
            await asyncio.sleep(0)
        await set_up(*args)

    monkeypatch.setattr(event_platform, "async_setup_entry", cold_setup)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert fired == ["rest_ended"]


async def test_entities_belong_to_one_device_named_by_the_phone(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, entry = await pair(hass, raw_client)
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{ACCOUNT}_workout"
    )
    assert entity_id == "binary_sensor.raw_randy_workout"

    hello = phone.hello()
    hello["display_name"] = "Ana"
    await phone.post(hello)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ACCOUNT), entry.entry_id
    )
    assert device.name == "RAW Ana"
    # Entity ids are forever: the rename leaves them as they were made.
    assert er.async_get(hass).async_get(entity_id) is not None
