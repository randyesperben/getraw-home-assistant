"""The live workout as Home Assistant shows it (contract 1's ``live``, ADR 0173).

The phone's ``state`` and ``event`` messages carry the whole ``live`` object; a
lock-screen ``patch`` carries only what its tap moved and is merged into the
kept one here. What the entities show is then read at a given moment, because
two things change with no message at all: a rest ends at ``rest_ends_at`` and
a workout left alone ends at ``abandoned_at`` (RAW's rule, written by the phone).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Final

from .messages import Event, parse_instant

IDLE: Final[dict[str, Any]] = {"phase": "idle"}
PHASES: Final = ("idle", "active", "resting", "finishing")

# The texts that describe the set due or the one after it: stale once the set moves.
_SET_TEXTS: Final = ("set", "target", "previous", "next")
_SET_EFFORT: Final = ("target_rpe", "previous_rpe")


def merged(kept: dict[str, Any], patch: dict[str, Any], events: list[Event]) -> dict[str, Any]:
    """The live state after a lock-screen ``patch`` (contract 1, «The lock screen's patch»).

    The patch never carries a name, a target or a text (the card may be
    discreet), so the kept ones stay while they still describe the same
    exercise and set, and go when the tap moved past them (AUDIT-L1-1 I10): HA
    then shows them unknown until Dart's next message, never the old set's.
    """
    if kept.get("started_at") != patch.get("started_at"):
        kept = {}
    live = deepcopy(kept)
    for key in ("started_at", "phase", "completed_sets"):
        if key in patch:
            live[key] = patch[key]
    for key in ("rest_started_at", "rest_ends_at"):
        if key in patch:
            live[key] = patch[key]
        else:
            live.pop(key, None)
    text: dict[str, Any] = live.setdefault("text", {})
    effort: dict[str, Any] = live.get("health", {}).get("effort", {})
    if not _same(live, patch, "exercise"):
        text.pop("exercise", None)
    if not _same(live, patch, "set"):
        for key in _SET_TEXTS:
            text.pop(key, None)
        for key in _SET_EFFORT:
            effort.pop(key, None)
        live.pop("stopwatch_started_at", None)
    for key in ("exercise", "set"):
        if key not in patch:
            live.pop(key, None)
        elif _same(live, patch, key):
            live[key] = {**live[key], **patch[key]}
        else:
            live[key] = dict(patch[key])
    if not text:
        live.pop("text")
    _move_activity(live, events)
    return live


def _same(live: dict[str, Any], patch: dict[str, Any], key: str) -> bool:
    kept, moved = live.get(key), patch.get(key)
    return isinstance(kept, dict) and isinstance(moved, dict) and kept.get("id") == moved.get("id")


def _move_activity(live: dict[str, Any], events: list[Event]) -> None:
    """A tap is activity: move ``last_activity_at`` and ``abandoned_at`` together.

    The phone alone knows how long RAW waits before it calls a workout abandoned;
    keeping the gap it wrote keeps that rule in one place while lock-screen taps
    go on with RAW closed.
    """
    last = parse_instant(live.get("last_activity_at"))
    abandoned = parse_instant(live.get("abandoned_at"))
    if last is None or abandoned is None or not events:
        return
    newest = max(event.occurred_at for event in events)
    if newest <= last:
        return
    live["last_activity_at"] = instant(newest)
    live["abandoned_at"] = instant(abandoned + (newest - last))


def instant(moment: datetime) -> str:
    """A UTC moment as a contract instant (three-digit milliseconds)."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class LiveView:
    """The live state read at ``now``, its instants moved onto Home Assistant's clock."""

    def __init__(self, live: dict[str, Any], offset: timedelta, now: datetime) -> None:
        self._offset = offset
        self._now = now
        abandoned = self._at(live, "abandoned_at")
        phase = live.get("phase")
        self.live: dict[str, Any] = live
        if phase not in PHASES:
            # A phase a newer phone added is shown unknown, never guessed.
            self.live = {}
        elif phase == "idle" or (abandoned is not None and now >= abandoned):
            # An abandoned workout stays off until the phone moves its last activity.
            self.live = IDLE
        self.rest_end = self._at(self.live, "rest_ends_at")
        if self.live.get("phase") == "resting" and (self.rest_end is None or now >= self.rest_end):
            self.rest_end = None

    def _at(self, live: dict[str, Any], key: str) -> datetime | None:
        moment = parse_instant(live.get(key))
        return moment + self._offset if moment is not None else None

    @property
    def phase(self) -> str | None:
        """``resting`` turns ``active`` at the rest's end, as the phone does."""
        phase = self.live.get("phase")
        if phase == "resting" and self.rest_end is None:
            return "active"
        return phase

    @property
    def training(self) -> bool | None:
        phase = self.phase
        return None if phase is None else phase != "idle"

    @property
    def resting(self) -> bool | None:
        phase = self.phase
        return None if phase is None else phase == "resting"

    def at(self, key: str) -> datetime | None:
        """An instant of the workout on Home Assistant's clock."""
        return self._at(self.live, key)

    def part(self, *path: str) -> Any:
        """A value inside the live object, or None."""
        node: Any = self.live
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node
