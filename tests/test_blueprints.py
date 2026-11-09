"""The five blueprints, loaded and run by Home Assistant itself, and the sample dashboard.

A blueprint Home Assistant refuses, or one that fires the wrong scene, is what
a person sees after importing it, so each one is set up as an automation the
way the import dialog makes it and driven through the states and events RAW's
entities give. A dashboard card naming an entity the integration never makes
shows «Entity not available» to whoever pastes it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import re
from pathlib import Path
import shutil
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint import models
from homeassistant.components.sensor.recorder import DEFAULT_STATISTICS
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from homeassistant.util.yaml import load_yaml_dict

from pytest_homeassistant.common import async_fire_time_changed, async_mock_service

from custom_components.getraw.sensor import DESCRIPTIONS

_ROOT = Path(__file__).parent.parent
BLUEPRINTS = _ROOT / "blueprints"
FOLDER = BLUEPRINTS / "automation" / "getraw"

WORKOUT = "binary_sensor.raw_test_workout"
EVENTS = "event.raw_test_workout_events"
GOAL_MET = "binary_sensor.raw_test_weekly_goal_met"
REST_END = "sensor.raw_test_rest_end"
PERSON = "person.test"


def blueprint_files() -> list[Path]:
    files = sorted(FOLDER.glob("*.yaml"))
    if not files:
        raise AssertionError(f"no blueprints in {FOLDER}")
    return files


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """A config folder holding the blueprints where an import puts them."""
    shutil.copytree(BLUEPRINTS, Path(hass_tmp_config_dir) / "blueprints", dirs_exist_ok=True)
    return hass_tmp_config_dir


@pytest.mark.parametrize("path", blueprint_files(), ids=lambda p: p.name)
def test_each_blueprint_imports_from_the_public_repository(path: Path) -> None:
    """Valid for Home Assistant, re-importable from its own URL, gated at the minimum release."""
    blueprint = models.Blueprint(
        load_yaml_dict(str(path)), expected_domain="automation", schema=AUTOMATION_BLUEPRINT_SCHEMA
    )
    repository = json.loads(
        (_ROOT / "custom_components" / "getraw" / "manifest.json").read_text(encoding="utf-8")
    )["issue_tracker"].removesuffix("/issues")
    assert blueprint.metadata["source_url"] == (
        f"{repository}/blob/main/{path.relative_to(_ROOT).as_posix()}"
    )
    minimum = json.loads((_ROOT / "hacs.json").read_text(encoding="utf-8"))["homeassistant"]
    assert blueprint.metadata["homeassistant"]["min_version"] == minimum


def test_the_sample_dashboard_names_only_entities_the_integration_makes() -> None:
    """Every `raw_alex_` entity id is one RAW makes for a device named «RAW Alex» in English."""
    names = json.loads(
        (_ROOT / "custom_components" / "getraw" / "translations" / "en.json").read_text(encoding="utf-8")
    )["entity"]
    made = {
        f"{platform}.{slugify(f'RAW Alex {entity["name"]}')}"
        for platform, entities in names.items()
        for entity in entities.values()
    }
    text = (_ROOT / "dashboards" / "raw.yaml").read_text(encoding="utf-8")
    named = set(re.findall(r"\b[a-z_]+\.raw_alex_[a-z0-9_]+", text))
    assert named, "the dashboard names no RAW entity: not reading it"
    assert sorted(named - made) == []


# The recorder statistic each type a statistics graph asks for is drawn from: `change` is the difference of `sum`,
# and `state` is kept beside `sum` (the frontend's statistics chart).
_DRAWN_FROM = {"mean": "mean", "min": "min", "max": "max", "sum": "sum", "change": "sum", "state": "sum"}


def _cards(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        return [node, *(card for value in node.values() for card in _cards(value))]
    if isinstance(node, list):
        return [card for value in node for card in _cards(value)]
    return []


def test_the_sample_dashboard_charts_only_statistics_the_recorder_keeps() -> None:
    """A statistics graph asking for a type the recorder does not keep for the entity's state class draws nothing."""
    names = json.loads(
        (_ROOT / "custom_components" / "getraw" / "translations" / "en.json").read_text(encoding="utf-8")
    )["entity"]["sensor"]
    by_id = {
        f"sensor.{slugify(f'RAW Alex {names[d.key]["name"]}')}": d for d in DESCRIPTIONS if d.key in names
    }
    graphs = [
        card
        for card in _cards(load_yaml_dict(str(_ROOT / "dashboards" / "raw.yaml")))
        if card.get("type") == "statistics-graph"
    ]
    assert graphs, "the dashboard has no statistics graph: not reading it"
    for graph in graphs:
        for entity in graph["entities"]:
            kept = DEFAULT_STATISTICS[by_id[entity].state_class].types
            for stat in graph["stat_types"]:
                assert _DRAWN_FROM[stat] in kept, f"{graph['title']}: {stat} of {entity}, which keeps {sorted(kept)}"


async def _automation(
    hass: HomeAssistant, path: str, inputs: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    assert await async_setup_component(hass, "event", {})
    # The event entity as Home Assistant starts it: no event yet.
    hass.states.async_set(EVENTS, "unknown", {"event_types": ["rest_ended", "record"]})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "raw_test",
                "alias": "raw_test",
                "use_blueprint": {"path": f"getraw/{path}", "input": inputs},
            }
        },
    )
    await hass.async_block_till_done()
    state = hass.states.get("automation.raw_test")
    assert state is not None, f"{path}: no automation was made:\n{caplog.text}"
    assert state.state == "on", f"{path}: {state.attributes}"


def _fire_event(hass: HomeAssistant, event_type: str, attributes: dict[str, Any]) -> None:
    hass.states.async_set(
        EVENTS,
        dt_util.utcnow().isoformat(timespec="milliseconds"),
        {"event_type": event_type, "event_types": [event_type, "set_completed"], **attributes},
    )


async def _run_for(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Moves the clock in quarter seconds, letting the automation run between steps.

    ``async_block_till_done`` would wait for an automation paused in a delay, whose
    timer only this loop fires, so it yields instead and checks at the end that no
    run is left half done.
    """
    for _ in range(int(seconds * 4)):
        freezer.tick(timedelta(milliseconds=250))
        async_fire_time_changed(hass)
        for _ in range(20):
            await asyncio.sleep(0)
    automation = hass.states.get("automation.raw_test")
    assert automation is not None
    assert automation.attributes.get("current", 0) == 0, "an automation run is still in progress"
    await hass.async_block_till_done()


def _scene_ids(calls: list[ServiceCall]) -> list[str]:
    return [call.data["entity_id"] for call in calls]


async def test_workout_scenes_follow_the_workout_and_never_a_restart(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    turn_on = async_mock_service(hass, "scene", "turn_on")
    hass.states.async_set(PERSON, "home")
    hass.states.async_set(WORKOUT, "unavailable")
    await _automation(
        hass,
        "workout_scenes.yaml",
        {
            "workout": WORKOUT,
            "person": PERSON,
            "start_scene": "scene.gym_on",
            "end_scene": "scene.gym_off",
        },
        caplog,
    )

    # Home Assistant starting, or RAW's integration reloading: no scene.
    hass.states.async_set(WORKOUT, "on")
    await hass.async_block_till_done()
    hass.states.async_set(WORKOUT, "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set(WORKOUT, "off")
    await hass.async_block_till_done()
    assert turn_on == []

    hass.states.async_set(WORKOUT, "on")
    await hass.async_block_till_done()
    hass.states.async_set(WORKOUT, "off")
    await hass.async_block_till_done()
    assert _scene_ids(turn_on) == [["scene.gym_on"], ["scene.gym_off"]]

    # Away from home (a gym elsewhere), nothing at home changes.
    hass.states.async_set(PERSON, "not_home")
    hass.states.async_set(WORKOUT, "on")
    await hass.async_block_till_done()
    assert len(turn_on) == 2


async def test_rest_over_blinks_on_the_end_of_a_rest_and_puts_the_lights_back(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    create = async_mock_service(hass, "scene", "create")
    restore = async_mock_service(hass, "scene", "turn_on")
    toggle = async_mock_service(hass, "light", "toggle")
    chime = async_mock_service(hass, "test", "chime")
    hass.states.async_set(PERSON, "home")
    await _automation(
        hass,
        "rest_over.yaml",
        {
            "events": EVENTS,
            "person": PERSON,
            "lights": ["light.gym"],
            "blinks": 2,
            "also_do": [{"action": "test.chime"}],
        },
        caplog,
    )

    _fire_event(hass, "rest_started", {"seconds": 90})
    await _run_for(hass, freezer, 3)
    assert create == [] and toggle == [] and chime == []

    _fire_event(hass, "rest_ended", {})
    await _run_for(hass, freezer, 3)
    assert [call.data["snapshot_entities"] for call in create] == [["light.gym"]]
    assert len(toggle) == 4
    assert _scene_ids(restore) == [[f"scene.{create[0].data['scene_id']}"]]
    assert len(chime) == 1


async def test_record_lights_up_and_says_raws_own_sentence(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    async_mock_service(hass, "scene", "create")
    restore = async_mock_service(hass, "scene", "turn_on")
    light_on = async_mock_service(hass, "light", "turn_on")
    speak = async_mock_service(hass, "tts", "speak")
    hass.states.async_set(PERSON, "home")
    await _automation(
        hass,
        "record.yaml",
        {
            "events": EVENTS,
            "person": PERSON,
            "lights": ["light.gym"],
            "seconds": 2,
            "speakers": ["media_player.gym"],
            "voice": "tts.home",
        },
        caplog,
    )

    sentence = "Personal record in Bench Press: 82.5 kilograms, 8 reps"
    _fire_event(hass, "set_completed", {"text": "Bench Press, set 2 of 3: 82.5 kg × 8"})
    await _run_for(hass, freezer, 1)
    assert light_on == [] and speak == []

    _fire_event(hass, "record", {"text": sentence, "records": [{"kind": "max_weight"}]})
    await _run_for(hass, freezer, 3)
    assert [(call.data["entity_id"], call.data["rgb_color"]) for call in light_on] == [
        (["light.gym"], [255, 176, 0])
    ]
    assert len(restore) == 1
    assert [
        (call.data["entity_id"], call.data["media_player_entity_id"], call.data["message"])
        for call in speak
    ] == [(["tts.home"], ["media_player.gym"], sentence)]


async def test_record_with_lights_only_says_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    async_mock_service(hass, "scene", "create")
    async_mock_service(hass, "scene", "turn_on")
    light_on = async_mock_service(hass, "light", "turn_on")
    speak = async_mock_service(hass, "tts", "speak")
    hass.states.async_set(PERSON, "home")
    await _automation(
        hass,
        "record.yaml",
        {"events": EVENTS, "person": PERSON, "lights": ["light.gym"], "seconds": 1},
        caplog,
    )

    _fire_event(hass, "record", {"text": "Personal record in Squat: 100 kilograms"})
    await _run_for(hass, freezer, 2)
    assert len(light_on) == 1
    assert speak == []
    # Lights alone are a whole setup: the run ends without an error in the log or the trace.
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


# The time trigger keeps its next 18:00 scheduled when the test ends, as core's own time tests do.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_weekly_goal_reminds_only_on_its_day_while_the_goal_is_not_met(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    send = async_mock_service(hass, "notify", "send_message")
    zone = dt_util.get_default_time_zone()
    # Thursday 2026-11-12, 17:59 where Home Assistant is.
    freezer.move_to(dt_util.as_utc(dt_util.parse_datetime("2026-11-12T17:59:00").replace(tzinfo=zone)))
    hass.states.async_set(PERSON, "not_home")
    hass.states.async_set(GOAL_MET, "off")
    await _automation(
        hass,
        "weekly_goal.yaml",
        {"goal_met": GOAL_MET, "person": PERSON, "notify": ["notify.phone"]},
        caplog,
    )

    await _run_for(hass, freezer, 120)
    assert send == []  # Thursday

    freezer.move_to(dt_util.as_utc(dt_util.parse_datetime("2026-11-13T17:59:00").replace(tzinfo=zone)))
    await _run_for(hass, freezer, 120)
    assert [(call.data["entity_id"], call.data["title"]) for call in send] == [
        (["notify.phone"], "Weekly goal")
    ]

    # No goal in RAW («unknown»), or the goal met: no reminder.
    for state in ("unknown", "on"):
        hass.states.async_set(GOAL_MET, state)
        freezer.tick(timedelta(days=7) - timedelta(minutes=2))
        await _run_for(hass, freezer, 120)
    assert len(send) == 1


async def test_rest_timer_counts_down_raws_rest_and_cancels_with_it(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    start = async_mock_service(hass, "timer", "start")
    cancel = async_mock_service(hass, "timer", "cancel")
    hass.states.async_set(PERSON, "home")
    hass.states.async_set(REST_END, "unknown", {"device_class": "timestamp"})
    await _automation(
        hass,
        "rest_timer.yaml",
        {"rest_end": REST_END, "person": PERSON, "timer": "timer.rest"},
        caplog,
    )

    ends = dt_util.utcnow() + timedelta(seconds=89, milliseconds=600)
    hass.states.async_set(REST_END, ends.isoformat(), {"device_class": "timestamp"})
    await hass.async_block_till_done()
    assert [(call.data["entity_id"], call.data["duration"]) for call in start] == [
        (["timer.rest"], 90)
    ]

    # RAW moved the rest 30 s later: the timer follows.
    hass.states.async_set(
        REST_END, (ends + timedelta(seconds=30)).isoformat(), {"device_class": "timestamp"}
    )
    await hass.async_block_till_done()
    assert start[-1].data["duration"] == 120

    # Skipped: the sensor goes unknown and the timer stops.
    hass.states.async_set(REST_END, "unknown", {"device_class": "timestamp"})
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in cancel] == [["timer.rest"]]
