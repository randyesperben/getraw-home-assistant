"""RAW's Workouts calendar: past workouts and the days a routine is due (``entities.md``).

Past workouts come from the summary's ``calendar`` with their times; a planned
day has no time, so it is an all-day event on the phone's local date. With
Titles off the events take a generic name in Home Assistant's language, since
the phone sends no name then.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityDescription,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import RawEntity, RawEntityDescription, Reading
from .messages import parse_instant
from .runtime import RawConfigEntry
from .summary import parse_date


@dataclass(frozen=True, kw_only=True)
class RawCalendarDescription(CalendarEntityDescription, RawEntityDescription):
    """The Workouts calendar's description."""


DESCRIPTION = RawCalendarDescription(key="workouts", source="summary")
_GENERIC = f"component.{DOMAIN}.entity.calendar.workouts.state_attributes.message.state"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RawConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RawCalendar(entry, DESCRIPTION)])


class RawCalendar(RawEntity, CalendarEntity):
    """Read from the last summary each time Home Assistant asks."""

    @property
    def event(self) -> CalendarEvent | None:
        """The event on now, else the next one."""
        now = dt_util.now()
        upcoming = [e for e in self._events() if e.end_datetime_local > now]
        return min(upcoming, key=lambda e: e.start_datetime_local, default=None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [
            e
            for e in self._events()
            if e.start_datetime_local < end_date and e.end_datetime_local > start_date
        ]

    def _events(self) -> list[CalendarEvent]:
        reading = Reading.of(self.runtime)
        summary = reading.summary
        titles = reading.on("summary", "titles")
        events: list[CalendarEvent] = []
        workouts = summary.part("calendar", "workouts")
        for workout in workouts if isinstance(workouts, list) else []:
            if not isinstance(workout, dict):
                continue
            start = parse_instant(workout.get("started_at"))
            end = parse_instant(workout.get("finished_at"))
            if start is None or end is None or end <= start:
                continue
            name = summary.name("workouts", workout.get("id")) if titles else None
            events.append(
                CalendarEvent(
                    start=start,
                    end=end,
                    summary=name or self._generic("workout"),
                    uid=str(workout.get("id")),
                )
            )
        days = summary.part("days")
        for day in days if isinstance(days, list) else []:
            if not isinstance(day, dict) or day.get("plan") != "due":
                continue
            routine = day.get("next_routine")
            on = parse_date(day.get("date"))
            if on is None or on < summary.today or not isinstance(routine, dict):
                continue
            name = summary.name("routines", routine.get("id")) if titles else None
            events.append(
                CalendarEvent(
                    start=on,
                    end=on + timedelta(days=1),
                    summary=name or self._generic("planned"),
                    uid=f"{routine.get('id')}_{on.isoformat()}",
                )
            )
        return events

    def _generic(self, key: str) -> str:
        translation = f"{_GENERIC}.{key}"
        data = self.platform_data
        return (
            data.platform_translations.get(translation)
            or data.default_language_platform_translations.get(translation)
            or key
        )
