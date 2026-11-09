"""The history summary read at a given moment (``ha_summary.schema.json``, ADR 0173).

The phone sends today and the next 13 days, each as Home counts it on that day,
and sends again only when something changes. So Home Assistant turns the day
itself at midnight in the phone's zone, takes that day's figures, counts the
month's as 0 once the zone enters the next month, and shows ``unknown`` past
the last day it was sent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from homeassistant.util import dt as dt_util


def parse_date(value: Any) -> date | None:
    """A contract date (``YYYY-MM-DD``), or None."""
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


class SummaryView:
    """One summary, the day it says is today in its zone at ``now``."""

    def __init__(self, summary: dict[str, Any] | None, zone: tzinfo | None, now: datetime) -> None:
        self.summary = summary or {}
        self.zone = zone or dt_util.get_default_time_zone()
        self.today: date = now.astimezone(self.zone).date()
        today = self.today.isoformat()
        days = self.summary.get("days")
        self.day: dict[str, Any] | None = next(
            (day for day in days if isinstance(day, dict) and day.get("date") == today),
            None,
        ) if isinstance(days, list) else None

    @property
    def known(self) -> bool:
        return bool(self.summary)

    def part(self, *path: str) -> Any:
        """A value inside the summary, or None."""
        node: Any = self.summary
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    def today_part(self, *path: str) -> Any:
        """A value of today's entry, or None past the days the phone sent."""
        node: Any = self.day
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    @property
    def this_month(self) -> bool:
        """Whether the summary's month is the zone's current one."""
        return self.part("month", "month") == self.today.strftime("%Y-%m")

    def month_figure(self, *path: str) -> Any:
        """A month total: 0 once the zone enters a month the phone has not summed yet."""
        if not self.known:
            return None
        return self.part(*path) if self.this_month else 0

    def midnight(self, day: date) -> datetime:
        return datetime.combine(day, time(), tzinfo=self.zone)

    @property
    def month_start(self) -> datetime:
        return self.midnight(self.today.replace(day=1))

    @property
    def week_start(self) -> datetime | None:
        """The first day of today's week, RAW's ``first_day_of_week`` (ISO, 1 = Monday)."""
        first = self.part("first_day_of_week")
        if not isinstance(first, int) or not 1 <= first <= 7:
            return None
        back = (self.today.isoweekday() - first) % 7
        return self.midnight(self.today - timedelta(days=back))

    @property
    def next_midnight(self) -> datetime:
        return self.midnight(self.today + timedelta(days=1))

    def name(self, kind: str, item_id: Any) -> str | None:
        """A routine's or workout's name while titles are on (``health.titles``)."""
        names = self.part("health", "titles", kind)
        if not isinstance(names, dict) or not isinstance(item_id, str):
            return None
        name = names.get(item_id)
        return name if isinstance(name, str) and name else None
