"""RAW's binary sensors: training, resting, trained today, the weekly goal (contract 6)."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RawEntity, RawEntityDescription, Reading
from .runtime import RawConfigEntry


@dataclass(frozen=True, kw_only=True)
class RawBinarySensorDescription(BinarySensorEntityDescription, RawEntityDescription):
    """A binary sensor of the live workout or the summary."""


DESCRIPTIONS: tuple[RawBinarySensorDescription, ...] = (
    RawBinarySensorDescription(
        key="workout",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda r: r.live.training,
    ),
    RawBinarySensorDescription(key="resting", value_fn=lambda r: r.live.resting),
    RawBinarySensorDescription(
        key="trained_today",
        source="summary",
        value_fn=lambda r: r.summary.today_part("trained"),
    ),
    RawBinarySensorDescription(
        key="weekly_goal_met",
        source="summary",
        # Unknown without a goal: the phone sends goal_met only with one.
        value_fn=lambda r: r.summary.today_part("week", "goal_met"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RawConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(RawBinarySensor(entry, d) for d in DESCRIPTIONS)


class RawBinarySensor(RawEntity, BinarySensorEntity):
    entity_description: RawBinarySensorDescription

    @callback
    def _read(self, reading: Reading) -> None:
        super()._read(reading)
        value = self.entity_description.value_fn(reading)
        self._attr_is_on = value if isinstance(value, bool) else None
