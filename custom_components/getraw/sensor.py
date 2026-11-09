"""RAW's sensors: the live workout, the history summary and health (contract 6).

Weights are stored in kilograms and lengths in centimetres («nunca» 9); each is
shown in the person's unit in RAW, suggested to Home Assistant when the entity
is first created. Home Assistant reads a suggested unit only then, so the
sensors with a unit wait for the first summary, which carries the person's
units, before they are added (S-I1).

No count is ``total_increasing``: deleting or editing a workout lowers it. A
``total`` keeps its ``last_reset`` while its period is the same day's week or
month, across a new zone and a restart: the recorder starts a new cycle at any
other value and would count the period again (AUDIT-L2-3 M5).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfMass,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .entity import RawEntity, RawEntityDescription, Reading, present
from .live import PHASES
from .messages import parse_instant
from .runtime import RawConfigEntry, signal_update
from .summary import parse_date

type Units = Literal["weight", "length"]

PLANS = ["due", "done", "rest", "no_plan"]
FEELINGS = ["awful", "bad", "okay", "good", "great"]
LENGTHS = (
    "neck",
    "shoulders",
    "chest",
    "abdomen",
    "waist",
    "hips",
    "bicep_l",
    "bicep_r",
    "forearm_l",
    "forearm_r",
    "thigh_l",
    "thigh_r",
    "calf_l",
    "calf_r",
)


@dataclass(frozen=True, kw_only=True)
class RawSensorDescription(SensorEntityDescription, RawEntityDescription):
    """A sensor: its unit kind (suggested from the summary) and its period's start."""

    units: Units | None = None
    last_reset_fn: Callable[[Reading], datetime | None] | None = None


def _one_of(options: list[str], value: Any) -> str | None:
    return value if value in options else None


def _instant(reading: Reading, *path: str) -> datetime | None:
    return parse_instant(reading.summary.part(*path))


def _weight(key: str, **kwargs: Any) -> RawSensorDescription:
    return RawSensorDescription(
        key=key,
        units="weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=1,
        **kwargs,
    )


def _month(key: str, **kwargs: Any) -> RawSensorDescription:
    return RawSensorDescription(
        key=key,
        source="summary",
        state_class=SensorStateClass.TOTAL,
        last_reset_fn=lambda r: r.summary.month_start if r.summary.known else None,
        **kwargs,
    )


def _week(key: str) -> RawSensorDescription:
    return RawSensorDescription(
        key=key,
        source="summary",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda r: r.summary.today_part("week", key.removesuffix("_this_week")),
        last_reset_fn=lambda r: r.summary.week_start,
    )


def _measured(reading: Reading, *path: str) -> dict[str, Any]:
    return present(measured_on=reading.summary.part("health", *path, "measured_on"))


def _current_set(reading: Reading) -> dict[str, Any]:
    live = reading.live
    effort = reading.on("live", "effort")
    return present(
        total=live.part("set", "total"),
        set_type=live.part("set", "set_type"),
        target=live.part("text", "target"),
        dumbbells_in_play=live.part("set", "dumbbells_in_play"),
        target_rpe=live.part("health", "effort", "target_rpe") if effort else None,
        previous_rpe=live.part("health", "effort", "previous_rpe") if effort else None,
    )


def _last_workout(reading: Reading) -> dict[str, Any]:
    summary = reading.summary
    workout_id = summary.part("last_workout", "id")
    notes = summary.part("health", "notes", "last_workout")
    return present(
        title=summary.name("workouts", workout_id) if reading.on("summary", "titles") else None,
        sets=summary.part("last_workout", "sets"),
        reps=summary.part("last_workout", "reps"),
        records=summary.part("last_workout", "records"),
        notes=notes if reading.on("summary", "notes") else None,
    )


LIVE: tuple[RawSensorDescription, ...] = (
    RawSensorDescription(
        key="phase",
        device_class=SensorDeviceClass.ENUM,
        options=list(PHASES),
        value_fn=lambda r: r.live.phase,
    ),
    RawSensorDescription(
        key="workout_title",
        category="titles",
        value_fn=lambda r: r.live.part("health", "titles", "title"),
    ),
    RawSensorDescription(
        key="workout_started",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda r: r.live.at("started_at"),
    ),
    RawSensorDescription(
        key="current_exercise",
        value_fn=lambda r: r.live.part("exercise", "name"),
        attributes_fn=lambda r: present(
            index=r.live.part("exercise", "index"), total=r.live.part("exercise", "total")
        ),
    ),
    RawSensorDescription(
        key="current_set",
        value_fn=lambda r: r.live.part("set", "index"),
        attributes_fn=_current_set,
    ),
    _weight(
        "target_weight",
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.live.part("set", "target", "weight_kg"),
    ),
    RawSensorDescription(
        key="target_reps",
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.live.part("set", "target", "reps"),
        attributes_fn=lambda r: present(reps_max=r.live.part("set", "target", "reps_max")),
    ),
    RawSensorDescription(
        key="last_time",
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.live.part("text", "previous"),
    ),
    RawSensorDescription(
        key="next_set",
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.live.part("text", "next"),
    ),
    RawSensorDescription(
        key="sets_done",
        value_fn=lambda r: r.live.part("completed_sets"),
        attributes_fn=lambda r: present(total=r.live.part("total_sets")),
    ),
    RawSensorDescription(
        key="rest_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda r: r.live.rest_end,
    ),
    RawSensorDescription(
        key="timed_set_started",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.live.at("stopwatch_started_at"),
    ),
    _weight(
        "workout_volume",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.live.part("volume_kg"),
    ),
    RawSensorDescription(
        key="latest_record",
        value_fn=lambda r: r.live.part("text", "last_pr"),
        attributes_fn=lambda r: present(
            exercise=r.live.part("last_pr", "exercise_name"),
            kind=r.live.part("last_pr", "kind"),
            value=r.live.part("last_pr", "value"),
        ),
    ),
    RawSensorDescription(
        key="last_update",
        source="phone",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.runtime.guard.heard_at,
    ),
)

SUMMARY: tuple[RawSensorDescription, ...] = (
    RawSensorDescription(
        key="todays_plan",
        source="summary",
        device_class=SensorDeviceClass.ENUM,
        options=PLANS,
        value_fn=lambda r: _one_of(PLANS, r.summary.today_part("plan")),
        attributes_fn=lambda r: present(reason=r.summary.today_part("reason")),
    ),
    RawSensorDescription(
        key="next_routine",
        source="summary",
        category="titles",
        value_fn=lambda r: r.summary.name(
            "routines", r.summary.today_part("next_routine", "id")
        ),
    ),
    RawSensorDescription(
        key="next_routine_date",
        source="summary",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda r: parse_date(r.summary.today_part("next_routine", "date")),
    ),
    _week("training_days_this_week"),
    _week("workouts_this_week"),
    RawSensorDescription(
        key="weekly_goal",
        source="summary",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.summary.today_part("week", "goal"),
    ),
    RawSensorDescription(
        key="weeks_in_a_row",
        source="summary",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.summary.today_part("week", "weeks_in_a_row"),
    ),
    RawSensorDescription(
        key="last_workout",
        source="summary",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda r: _instant(r, "last_workout", "finished_at"),
        attributes_fn=_last_workout,
    ),
    RawSensorDescription(
        key="last_workout_duration",
        source="summary",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda r: r.summary.part("last_workout", "duration_sec"),
    ),
    _month("workouts_this_month", value_fn=lambda r: r.summary.month_figure("month", "workouts")),
    _month(
        "training_days_this_month",
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.summary.month_figure("month", "training_days"),
    ),
    _month(
        "training_time_this_month",
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value_fn=lambda r: r.summary.month_figure("month", "training_time_sec"),
    ),
    _month(
        "sets_this_month",
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.summary.month_figure("month", "sets"),
    ),
    _month(
        "reps_this_month",
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.summary.month_figure("month", "reps"),
    ),
    _month("records_this_month", value_fn=lambda r: r.summary.month_figure("month", "records")),
    RawSensorDescription(
        key="workouts_all_time",
        source="summary",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda r: r.summary.part("all_time", "workouts"),
    ),
)

HEALTH: tuple[RawSensorDescription, ...] = (
    _weight(
        "body_weight",
        source="summary",
        category="body_weight",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.summary.part("health", "body_weight", "weight_kg"),
        attributes_fn=lambda r: _measured(r, "body_weight"),
    ),
    RawSensorDescription(
        key="body_fat_pct",
        source="summary",
        category="measurements",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.summary.part("health", "measurements", "body_fat_pct", "value_pct"),
        attributes_fn=lambda r: _measured(r, "measurements", "body_fat_pct"),
    ),
    *(
        RawSensorDescription(
            key=length,
            source="summary",
            category="measurements",
            units="length",
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.CENTIMETERS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            entity_registry_enabled_default=False,
            value_fn=lambda r, k=length: r.summary.part("health", "measurements", k, "value_cm"),
            attributes_fn=lambda r, k=length: _measured(r, "measurements", k),
        )
        for length in LENGTHS
    ),
    RawSensorDescription(
        key="last_workout_effort",
        source="summary",
        category="effort",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.summary.part("health", "effort", "last_workout", "session_rpe"),
    ),
    RawSensorDescription(
        key="last_workout_feeling",
        source="summary",
        category="effort",
        device_class=SensorDeviceClass.ENUM,
        options=FEELINGS,
        value_fn=lambda r: _one_of(
            FEELINGS, r.summary.part("health", "effort", "last_workout", "feeling")
        ),
    ),
    _weight(
        "last_workout_volume",
        source="summary",
        category="saved_volume",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.summary.part("health", "saved_volume", "last_workout_kg"),
    ),
    _weight(
        "volume_this_month",
        source="summary",
        category="saved_volume",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda r: r.summary.month_figure("health", "saved_volume", "month_kg"),
        last_reset_fn=lambda r: r.summary.month_start if r.summary.known else None,
    ),
)

DESCRIPTIONS = LIVE + SUMMARY + HEALTH

_SUGGESTED: dict[tuple[Units, str], str] = {
    ("weight", "lb"): UnitOfMass.POUNDS,
    ("weight", "kg"): UnitOfMass.KILOGRAMS,
    ("length", "in"): UnitOfLength.INCHES,
    ("length", "cm"): UnitOfLength.CENTIMETERS,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RawConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors; those with a unit once the person's units are known."""
    runtime = entry.runtime_data
    now = [d for d in DESCRIPTIONS if d.units is None or runtime.summary is not None]
    later = [d for d in DESCRIPTIONS if d not in now]
    async_add_entities(_sensor(entry, d) for d in now)
    if not later:
        return

    @callback
    def add_once_units_known() -> None:
        if runtime.summary is None or not later:
            return
        async_add_entities(_sensor(entry, d) for d in later)
        later.clear()

    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_update(entry.entry_id), add_once_units_known)
    )


def _sensor(entry: RawConfigEntry, description: RawSensorDescription) -> RawSensor:
    if description.last_reset_fn is not None:
        return RawPeriodSensor(entry, description)
    return RawSensor(entry, description)


class RawSensor(RawEntity, SensorEntity):
    entity_description: RawSensorDescription
    # Health text the recorder never keeps: it would outlive its switch.
    _unrecorded_attributes = frozenset({"notes", "title"})

    def __init__(self, entry: RawConfigEntry, description: RawSensorDescription) -> None:
        super().__init__(entry, description)
        if description.units is not None and (summary := entry.runtime_data.summary):
            unit_key = "weight_unit" if description.units == "weight" else "length_unit"
            self._attr_suggested_unit_of_measurement = _SUGGESTED.get(
                (description.units, summary.get(unit_key)), description.native_unit_of_measurement
            )

    @callback
    def _read(self, reading: Reading) -> None:
        super()._read(reading)
        self._attr_native_value = self.entity_description.value_fn(reading)


class RawPeriodSensor(RawSensor, RestoreEntity):
    """A ``total`` of a week or a month, which starts again at ``last_reset``."""

    _attr_last_reset: datetime | None = None

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and isinstance(reset := last.attributes.get("last_reset"), str):
            self._attr_last_reset = dt_util.parse_datetime(reset)
        await super().async_added_to_hass()

    @callback
    def _read(self, reading: Reading) -> None:
        super()._read(reading)
        assert self.entity_description.last_reset_fn is not None
        start = self.entity_description.last_reset_fn(reading)
        kept = self._attr_last_reset
        # The same period's first day keeps its first instant.
        if start is None or kept is None or kept.date() != start.date():
            self._attr_last_reset = start
