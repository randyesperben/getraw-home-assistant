"""What every RAW entity shares (``entities.md``).

One device per RAW account; each entity reads the runtime when it is told
something changed. An entity under a health category is unavailable while the
phone says that category is off, so Home Assistant never shows a value the
person stopped sharing as if it were current.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.util import dt as dt_util

from .const import CONF_APP_VERSION, CONF_DISPLAY_NAME, DOMAIN
from .live import LiveView
from .runtime import RawConfigEntry, RawRuntime, signal_update
from .summary import SummaryView

type Source = Literal["live", "summary", "phone"]


@dataclass(frozen=True, slots=True)
class Reading:
    """The runtime read once, at one moment, for one entity's state."""

    runtime: RawRuntime
    live: LiveView
    summary: SummaryView
    now: datetime

    @classmethod
    def of(cls, runtime: RawRuntime) -> Reading:
        now = dt_util.utcnow()
        return cls(runtime, runtime.live_view(now), runtime.summary_view(now), now)

    def on(self, source: Source, category: str) -> bool:
        return self.runtime.category_on(source, category)


@dataclass(frozen=True, kw_only=True)
class RawEntityDescription(EntityDescription):
    """Where an entity's value comes from and the health category it needs."""

    source: Source = "live"
    category: str | None = None
    value_fn: Callable[[Reading], Any] = lambda _: None
    attributes_fn: Callable[[Reading], dict[str, Any]] | None = None


def entry_title(display_name: str | None) -> str:
    """«RAW Randy», or «RAW» for a profile without a name."""
    return f"RAW {display_name}" if display_name else "RAW"


def device_info(entry: RawConfigEntry) -> DeviceInfo:
    """«RAW Randy»: the account's one device, renamed by the phone's ``hello``.

    Named from the phone, never from the entry's title, which the person may
    have renamed.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name=entry_title(entry.data.get(CONF_DISPLAY_NAME)),
        manufacturer="Fabrica Studio",
        model="RAW",
        sw_version=entry.data.get(CONF_APP_VERSION),
        entry_type=DeviceEntryType.SERVICE,
    )


class RawEntity(Entity):
    """An entity of the account's device, written again on every update."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: RawEntityDescription

    def __init__(self, entry: RawConfigEntry, description: RawEntityDescription) -> None:
        self.entity_description = description
        self.runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_translation_key = description.translation_key or description.key
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self.runtime.entry_id), self._handle_update
            )
        )
        self._read(Reading.of(self.runtime))

    @callback
    def _handle_update(self) -> None:
        self._read(Reading.of(self.runtime))
        self.async_write_ha_state()

    @callback
    def _read(self, reading: Reading) -> None:
        """Set the entity's ``_attr_`` values from ``reading``."""
        description = self.entity_description
        self._attr_available = not self.runtime.refused_version and (
            description.category is None or reading.on(description.source, description.category)
        )
        if description.attributes_fn is not None:
            self._attr_extra_state_attributes = description.attributes_fn(reading)


def present(**values: Any) -> dict[str, Any]:
    """Attributes without the ones that have nothing to say."""
    return {key: value for key, value in values.items() if value is not None}
