"""RAW's one event entity: every workout event, for ``trigger: event.received`` (``entities.md``).

An event's attributes are its ``data`` and its ``text``, never its
``health``: Home Assistant shows the last event's attributes until the next
event and restores them after a restart, and has no way to clear them without
firing one, so a switch turned off could not hide them. Each
health value has its own entity under its category instead.

Its instants are moved onto Home Assistant's clock, as every sensor's are, so
an automation on a rest's ``ends_at`` and one on Rest end agree.

Home Assistant's own ``rest_ended`` and ``workout_abandoned`` are claimed here,
when they fire, so one that comes due while no entity listens is never spent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import EVENT_TYPES, HOME_ASSISTANT_EVENT_TYPES
from .entity import RawEntity, RawEntityDescription
from .live import instant
from .messages import Event, parse_instant
from .runtime import RawConfigEntry, signal_events


@dataclass(frozen=True, kw_only=True)
class RawEventDescription(EventEntityDescription, RawEntityDescription):
    """The event entity's description."""


DESCRIPTION = RawEventDescription(key="events")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RawConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RawEvents(entry, DESCRIPTION)])


def attributes_of(event: Event, offset: timedelta) -> dict[str, Any]:
    """What an automation reads off ``trigger.to_state.attributes``.

    Every instant in the event's ``data`` is a phone instant: it is shown plus
    ``offset``, in the contract's own form.
    """
    raw = event.raw
    data = raw.get("data")
    attributes: dict[str, Any] = {
        key: instant(at + offset) if (at := parse_instant(value)) is not None else value
        for key, value in (data.items() if isinstance(data, dict) else ())
    }
    if isinstance(text := raw.get("text"), str):
        attributes["text"] = text
    return attributes


class RawEvents(RawEntity, EventEntity):
    """Fires what the phone and Home Assistant's own timers say happened."""

    _attr_event_types = list(EVENT_TYPES)
    # A set's figures and a record's list change with every event; the
    # recorder keeps the type, never them.
    _unrecorded_attributes = frozenset({"records", "values"})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_events(self.runtime.entry_id), self._fire
            )
        )

    @callback
    def _fire(self, events: list[Event]) -> None:
        now = dt_util.utcnow()
        for event in events:
            if event.type not in EVENT_TYPES:
                continue
            if event.type in HOME_ASSISTANT_EVENT_TYPES and not self.runtime.guard.claim(
                event.event_id, now
            ):
                continue
            self._trigger_event(event.type, attributes_of(event, self.runtime.offset))
            self.async_write_ha_state()
