"""What one paired phone has told Home Assistant, shared by the webhook and the entities.

The webhook applies each newer message here; the entities read it at the moment
they write their state. Three things change with no message, so this module
keeps a timer for each: the rest's end (Home Assistant fires ``rest_ended``),
``abandoned_at`` (it fires ``workout_abandoned``) and midnight in the phone's
zone (the summary's next day). The last live state and summary are kept in the
entry's store, so a restart shows what was shown before it; the timers are armed
only once the entities listen, so an end that passed while Home Assistant started
still fires.

Health a body carries for a category its own ``categories`` leaves off is
dropped before anything is kept: the store goes into backups, so it holds only
what the phone shares.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, tzinfo
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import DOMAIN, HEALTH_CATEGORIES
from .guard import EVENT_FRESH, ReplayGuard
from .live import IDLE, LiveView, merged
from .messages import Event, parse_instant
from .summary import SummaryView

# Home Assistant rings the rest's end itself. Later than this (a restart across
# it, a state that arrives late) the flash would be wrong, so it stays silent.
REST_END_LATE: Final = timedelta(seconds=10)
# A plain state that ends a rest this close to its end is the phone's own rest
# timer running out on a clock a little ahead, not a skip (a skip, a set or the
# end of the workout come as events), so it rings the rest's end now.
REST_END_EARLY: Final = timedelta(seconds=5)


def signal_update(entry_id: str) -> str:
    """Dispatched whenever what the entities show may have changed."""
    return f"{DOMAIN}_{entry_id}_update"


def signal_events(entry_id: str) -> str:
    """Dispatched with the events to fire, in the order they happened."""
    return f"{DOMAIN}_{entry_id}_events"


def _ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


class RawRuntime:
    """One entry's live view: the webhook writes it, the entities read it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        webhook_id: str,
        guard: ReplayGuard,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.webhook_id = webhook_id
        self.guard = guard
        self.live: dict[str, Any] = dict(IDLE)
        self.summary: dict[str, Any] | None = None
        self.zone: tzinfo | None = None
        # The health categories on, as the last message of each source said,
        # oldest first (see ``category_on``).
        self._categories: dict[str, frozenset[str]] = {}
        # Set while the phone writes a schema_version this release cannot read:
        # the entities show unavailable rather than stale figures.
        self.refused_version = False
        self._timers: list[CALLBACK_TYPE] = []

    async def async_restore(self) -> None:
        """Show what the store kept; ``arm`` once the entities listen."""
        view = self.guard.view
        if isinstance(live := view.get("live"), dict):
            self.live = live
        if isinstance(summary := view.get("summary"), dict):
            self.summary = summary
            self.zone = await zone_of(summary)
        kept = view.get("categories")
        for source, categories in kept if isinstance(kept, list) else []:
            self._categories[source] = frozenset(categories)

    @callback
    def stop(self) -> None:
        for cancel in self._timers:
            cancel()
        self._timers.clear()

    @property
    def offset(self) -> timedelta:
        """The phone's clock to Home Assistant's (the guard's offset)."""
        return timedelta(milliseconds=self.guard.offset_ms or 0)

    def live_view(self, now: datetime | None = None) -> LiveView:
        return LiveView(self.live, self.offset, now or dt_util.utcnow())

    def summary_view(self, now: datetime | None = None) -> SummaryView:
        return SummaryView(self.summary, self.zone, now or dt_util.utcnow())

    def category_on(self, source: str, category: str) -> bool:
        """Whether ``source``'s figures for ``category`` may show.

        On only while that source's last list and every list received after it
        say so: a switch turned off reaches HA in the next ``state``, and the
        summary's figures then hide too, before the next summary comes.
        """
        sources = list(self._categories)
        if source not in sources:
            return False
        return all(
            shares(category, self._categories[s]) for s in sources[sources.index(source) :]
        )

    def _heard_categories(self, source: str, body: dict[str, Any]) -> None:
        self._categories.pop(source, None)
        self._categories[source] = categories_of(body)

    @callback
    def apply(
        self,
        kind: str,
        body: dict[str, Any] | None,
        *,
        events: Sequence[Event] = (),
        fire: Sequence[Event] = (),
        zone: tzinfo | None = None,
    ) -> None:
        """Show a message: ``body`` when it is newer, then fire ``fire``.

        The state goes first, so an automation triggered by an event reads the
        state that event left (an ``event`` message carries the state after it).
        ``events`` are all the message's events, ``fire`` the fresh ones.
        """
        now = dt_util.utcnow()
        fired = list(fire)
        if body is not None:
            before = self.live
            match kind:
                case "state" | "event":
                    self.live = shared(body["live"], categories_of(body))
                    self._heard_categories("live", body)
                case "patch":
                    self.live = merged(self.live, body["live"], list(events))
                case "bye":
                    self.live = dict(IDLE)
                case "summary":
                    self.summary = shared(body, categories_of(body))
                    self.zone = zone
                    self._heard_categories("summary", body)
            if kind == "state" and (rang := self._rest_ran_out(before, now)):
                fired.insert(0, rang)
            self._keep()
            self.arm()
        async_dispatcher_send(self.hass, signal_update(self.entry_id))
        if fired:
            async_dispatcher_send(self.hass, signal_events(self.entry_id), fired)

    def _keep(self) -> None:
        self.guard.keep_view(
            {
                "live": self.live,
                "summary": self.summary,
                "categories": [[k, sorted(v)] for k, v in self._categories.items()],
            }
        )

    def _rest_ran_out(self, before: dict[str, Any], now: datetime) -> Event | None:
        """``rest_ended`` when a plain state ends a rest at its end."""
        end = self._rest_end(before)
        if end is None or self._rest_end(self.live) == end:
            return None
        if not end - REST_END_EARLY <= now < end + REST_END_LATE:
            return None
        return _rest_ended(before)

    def _rest_end(self, live: dict[str, Any]) -> datetime | None:
        if live.get("phase") != "resting":
            return None
        end = parse_instant(live.get("rest_ends_at"))
        return end + self.offset if end is not None else None

    @callback
    def arm(self) -> None:
        """Set the timers for what the kept state says comes next."""
        self.stop()
        now = dt_util.utcnow()
        if (end := self._rest_end(self.live)) is not None and now < end + REST_END_LATE:
            self._at(end, self._ring(_rest_ended(self.live), end, REST_END_LATE))
        abandoned = parse_instant(self.live.get("abandoned_at"))
        if self.live.get("phase") != "idle" and abandoned is not None:
            due = abandoned + self.offset
            if now < due + EVENT_FRESH:
                self._at(due, self._ring(_abandoned(self.live), due, EVENT_FRESH))
        if self.summary is not None:
            self._at(self.summary_view(now).next_midnight, self._day_turned)

    def _at(self, moment: datetime, action: Any) -> None:
        self._timers.append(async_track_point_in_utc_time(self.hass, action, moment))

    def _ring(self, event: Event | None, due: datetime, late: timedelta) -> Any:
        @callback
        def ring(_scheduled: datetime) -> None:
            # The timer hands back the time it was set for; a host that slept
            # runs it late, so the clock decides whether it still rings. The
            # event entity claims it as it fires it, so it rings once.
            due_now = event is not None and dt_util.utcnow() < due + late
            async_dispatcher_send(self.hass, signal_update(self.entry_id))
            if due_now:
                async_dispatcher_send(self.hass, signal_events(self.entry_id), [event])

        return ring

    @callback
    def _day_turned(self, now: datetime) -> None:
        self.arm()
        async_dispatcher_send(self.hass, signal_update(self.entry_id))


def categories_of(body: dict[str, Any]) -> frozenset[str]:
    categories = body.get("categories")
    if not isinstance(categories, list):
        return frozenset()
    return frozenset(c for c in categories if isinstance(c, str))


def shares(category: str, categories: frozenset[str]) -> bool:
    """Whether a list of categories shares ``category``: Saved volume needs Body weight too.

    Saved volume counts the body weight in bodyweight sets, so it would tell
    that weight with Body weight off.
    """
    return (
        category in HEALTH_CATEGORIES
        and category in categories
        and (category != "saved_volume" or "body_weight" in categories)
    )


def shared(node: dict[str, Any], categories: frozenset[str]) -> dict[str, Any]:
    """``node`` with only the ``health`` its categories share; no ``health`` when none."""
    health = node.get("health")
    kept = (
        {c: v for c, v in health.items() if shares(c, categories)}
        if isinstance(health, dict)
        else {}
    )
    rest = {k: v for k, v in node.items() if k != "health"}
    return rest | {"health": kept} if kept else rest


async def zone_of(summary: dict[str, Any]) -> tzinfo | None:
    """The summary's IANA zone, read off the event loop when it is not cached."""
    name = summary.get("time_zone")
    return await dt_util.async_get_time_zone(name) if isinstance(name, str) else None


def _home_assistant_event(
    live: dict[str, Any], type_: str, key: str, data: dict[str, Any]
) -> Event | None:
    """An event Home Assistant fires itself, its id built as the phone builds its events' ids."""
    started = parse_instant(live.get("started_at"))
    at = parse_instant(live.get(key))
    if started is None or at is None:
        return None
    return Event(
        event_id=f"{_ms(started)}:{type_}:{_ms(at)}",
        type=type_,
        occurred_at=at,
        raw={"data": {k: v for k, v in data.items() if v is not None}},
    )


def _rest_ended(live: dict[str, Any]) -> Event | None:
    return _home_assistant_event(
        live,
        "rest_ended",
        "rest_ends_at",
        {"rest_started_at": live.get("rest_started_at"), "ends_at": live.get("rest_ends_at")},
    )


def _abandoned(live: dict[str, Any]) -> Event | None:
    return _home_assistant_event(
        live, "workout_abandoned", "abandoned_at", {"abandoned_at": live.get("abandoned_at")}
    )


type RawConfigEntry = ConfigEntry[RawRuntime]
