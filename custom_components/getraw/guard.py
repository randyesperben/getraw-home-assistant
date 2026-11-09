"""The replay guard: what Home Assistant already applied, kept across restarts.

Per entry, in a ``Store`` (``.storage/getraw.<entry id>``): the highest revision
applied per sequence (``live`` and ``summary``), the event ids seen
lately, the phone's clock offset, when the phone was last heard, and the last
``bye``. Restore data is written only every 15 minutes, so a power cut would roll
it back that far and let a captured message apply twice; the store is saved 2 s
after a change and at stop, since a write per post would wear the SD card many
Home Assistant hosts run on.

The same store keeps the last live state and summary applied (``view``), so
the entities, the timers and the calendar show the same after a restart as
before it, in one write with the guard.

A new pairing (another webhook id) starts a clean guard, so a phone whose clock
runs behind the last one's is never ignored.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
import logging
from typing import Any, Final, NotRequired, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .messages import Event

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1
SAVE_DELAY_SECONDS: Final = 2

# HA fires an event only while it is under this old on the corrected clock.
EVENT_FRESH: Final = timedelta(minutes=2)
# How long a seen id is kept after receipt: the freshness window with room for
# the offset to move, so a repeat inside it is always caught.
SEEN_KEPT: Final = timedelta(minutes=10)
SEEN_MAX: Final = 2000
# Newer messages in a row, each sampling over EVENT_FRESH above the offset,
# that say the two clocks moved apart (a phone clock set back, NTP fixing a
# phone that ran ahead) rather than one post held on the way.
CLOCK_APART_MESSAGES: Final = 3


class _Saved(TypedDict):
    webhook_id: str
    revisions: dict[str, int]
    seen: dict[str, int]
    offset_ms: int | None
    heard_at: str | None
    bye: str | None
    view: NotRequired[dict[str, Any]]


def _ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


class ReplayGuard:
    """Revisions, seen events, the clock offset and the phone's last word, for one entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str, webhook_id: str) -> None:
        self._store: Store[_Saved] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True
        )
        self._webhook_id = webhook_id
        self._revisions: dict[str, int] = {}
        self._seen: dict[str, int] = {}
        self.offset_ms: int | None = None
        self.heard_at: datetime | None = None
        self.bye: str | None = None
        self._view: dict[str, Any] = {}
        # Kept in memory only: a restart starts the count again.
        self._apart = 0

    async def async_load(self) -> None:
        """Read the saved guard; one saved for another pairing is dropped."""
        saved = await self._store.async_load()
        if saved is None or saved.get("webhook_id") != self._webhook_id:
            return
        self._revisions = dict(saved["revisions"])
        self._seen = dict(saved["seen"])
        self.offset_ms = saved["offset_ms"]
        self.heard_at = dt_util.parse_datetime(saved["heard_at"] or "")
        self.bye = saved["bye"]
        self._view = dict(saved.get("view") or {})

    @property
    def view(self) -> dict[str, Any]:
        """The last live state and summary applied, as the runtime kept them."""
        return self._view

    def keep_view(self, view: dict[str, Any]) -> None:
        self._view = view
        self._save()

    def start_silence_clock(self, now: datetime) -> None:
        """A new pairing counts its 14 days of silence from its first setup."""
        if self.heard_at is None:
            self.heard_at = now
            self._save()

    async def async_flush(self) -> None:
        """Write now (at unload), so a reload never forgets the last second."""
        await self._store.async_save(self._saved())

    async def async_remove(self) -> None:
        """Delete the saved guard with its entry."""
        await self._store.async_remove()

    def is_newer(self, sequence: str, revision: int) -> bool:
        """Whether ``revision`` is above everything applied in ``sequence``."""
        applied = self._revisions.get(sequence)
        return applied is None or revision > applied

    def heard(
        self,
        *,
        sequence: str,
        revision: int,
        phone_now: datetime,
        received: datetime,
        bye: str | None = None,
    ) -> bool:
        """Record a message that opened and is this entry's; True when it is newer.

        Only a newer message counts as the phone heard: a replay of an old one
        moves nothing.

        The clock offset is the smallest receipt minus ``phone_now`` this pairing
        has seen, and only a newer message may lower it. It never goes up: a
        newer message held back on the way would otherwise make itself look
        young and fire its events late. The cost, accepted because the fix (Home
        Assistant's own time in every ack) would change both sides for a rare
        case: a phone clock moved back over 2 minutes then loses its events and
        shows its times early until the next pairing, which starts a clean guard;
        ``clocks_apart`` says when that happened.
        """
        newer = self.is_newer(sequence, revision)
        if not newer:
            return False
        self.heard_at = received
        self._revisions[sequence] = revision
        sample = _ms(received) - _ms(phone_now)
        if self.offset_ms is None or sample < self.offset_ms:
            self.offset_ms = sample
        apart = sample - self.offset_ms > EVENT_FRESH.total_seconds() * 1000
        self._apart = self._apart + 1 if apart else 0
        if sequence == "live":
            self.bye = bye
        self._save()
        return True

    @property
    def clocks_apart(self) -> bool:
        """Whether the last newer messages all say the clocks moved apart since pairing.

        The offset stays where it is, so a held-back message still cannot fire
        late; only showing the QR code again starts a clean one.
        """
        return self._apart >= CLOCK_APART_MESSAGES

    def fresh(self, events: Iterable[Event], received: datetime) -> list[Event]:
        """The events to fire: never seen, and under 2 minutes old on the corrected clock."""
        now_ms = _ms(received)
        self._prune(now_ms)
        offset = self.offset_ms or 0
        fresh: list[Event] = []
        for event in events:
            if event.event_id in self._seen:
                _LOGGER.debug("event %s seen before, not fired", event.type)
                continue
            self._seen[event.event_id] = now_ms
            age_ms = now_ms - (_ms(event.occurred_at) + offset)
            fires = age_ms < EVENT_FRESH.total_seconds() * 1000
            if fires:
                fresh.append(event)
            # The device pass reads this line (dev/devpass.py watch): how long after
            # the phone's moment, on Home Assistant's clock, the event arrived. The
            # type only: never an id, a name or a value.
            _LOGGER.debug(
                "event %s %s, %d ms after it happened",
                event.type,
                "fired" if fires else "too old, not fired",
                age_ms,
            )
        self._save()
        return fresh

    def claim(self, event_id: str, now: datetime) -> bool:
        """Whether an event Home Assistant fires itself is due: True once per id.

        Its timer may be armed again by a restart or a newer state; the seen ids
        outlive every window it fires in, so it never fires twice.
        """
        now_ms = _ms(now)
        self._prune(now_ms)
        if event_id in self._seen:
            return False
        self._seen[event_id] = now_ms
        self._save()
        return True

    def corrected(self, phone_time: datetime) -> datetime:
        """A phone instant on Home Assistant's clock."""
        return phone_time + timedelta(milliseconds=self.offset_ms or 0)

    def _prune(self, now_ms: int) -> None:
        horizon = now_ms - SEEN_KEPT.total_seconds() * 1000
        self._seen = {i: at for i, at in self._seen.items() if at >= horizon}
        if len(self._seen) > SEEN_MAX:
            newest = sorted(self._seen.items(), key=lambda item: item[1])[-SEEN_MAX:]
            self._seen = dict(newest)

    def _save(self) -> None:
        self._store.async_delay_save(self._saved, SAVE_DELAY_SECONDS)

    def _saved(self) -> _Saved:
        return {
            "webhook_id": self._webhook_id,
            "revisions": dict(self._revisions),
            "seen": dict(self._seen),
            "offset_ms": self.offset_ms,
            "heard_at": self.heard_at.isoformat() if self.heard_at else None,
            "bye": self.bye,
            "view": self._view,
        }
