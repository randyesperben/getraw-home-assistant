"""RAW: one device per RAW account, fed by the phone's sealed posts (ADR 0173)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from homeassistant.components.webhook import async_unregister as webhook_unregister
from homeassistant.const import CONF_WEBHOOK_ID, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import cloudhook, webhook as raw_webhook
from .const import DOMAIN
from .guard import ReplayGuard
from .runtime import RawConfigEntry, RawRuntime
from .webhook import ISSUE_SILENT, async_raise_issue, issue_id

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
    Platform.EVENT,
    Platform.SENSOR,
]

# No message for this long raises a repair, unless the phone said it paused (P-M8).
SILENCE: Final = timedelta(days=14)


async def async_setup_entry(hass: HomeAssistant, entry: RawConfigEntry) -> None:
    """Listen on the entry's webhook with its replay guard loaded."""
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    guard = ReplayGuard(hass, entry.entry_id, webhook_id)
    await guard.async_load()
    guard.start_silence_clock(dt_util.utcnow())
    runtime = RawRuntime(hass, entry.entry_id, webhook_id, guard)
    await runtime.async_restore()
    entry.async_on_unload(runtime.stop)
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Only now: a rest's end due while the platforms load would otherwise ring
    # before the event entity listens (AUDIT-L2-3 C2).
    runtime.arm()
    raw_webhook.async_register(hass, entry)
    await cloudhook.async_setup(hass, entry)

    @callback
    def check_silence(now: datetime | None = None) -> None:
        _check_silence(hass, entry)

    check_silence()
    entry.async_on_unload(
        async_track_time_interval(hass, check_silence, timedelta(hours=1))
    )


async def async_unload_entry(hass: HomeAssistant, entry: RawConfigEntry) -> None:
    """Write the replay guard now, so a reload never forgets the last seconds.

    The timers and the webhook stop first, while the entities still listen:
    nothing then rings to no one or writes the guard after its last write
    (AUDIT-L2-3 C2, AUDIT-L2-2 M1).
    """
    runtime = entry.runtime_data
    runtime.stop()
    webhook_unregister(hass, runtime.webhook_id)
    await runtime.guard.async_flush()
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: RawConfigEntry) -> None:
    """The phone learns it was removed; its guard and cloudhook go.

    Home Assistant deletes the entry's repairs itself (``config_entry_id``).
    """
    raw_webhook.async_register_tombstone(hass, entry.data)
    await ReplayGuard(hass, entry.entry_id, entry.data[CONF_WEBHOOK_ID]).async_remove()
    await cloudhook.async_delete(hass, entry.data)


@callback
def _check_silence(hass: HomeAssistant, entry: RawConfigEntry) -> None:
    guard = entry.runtime_data.guard
    silent = (
        guard.bye is None
        and guard.heard_at is not None
        and dt_util.utcnow() - guard.heard_at > SILENCE
    )
    if not silent:
        ir.async_delete_issue(hass, DOMAIN, issue_id(ISSUE_SILENT, entry.entry_id))
        return
    async_raise_issue(hass, entry, ISSUE_SILENT)
