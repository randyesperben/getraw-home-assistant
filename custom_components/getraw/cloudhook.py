"""The Home Assistant Cloud route, kept as mobile_app keeps its own.

Created at setup and on each reconnection when the person subscribes, updated
when the cloud changes it, dropped at logout; pairing never waits for the cloud
and never fails for it. ``cloud`` is imported only once Home Assistant loaded it,
as core's ``WebhookFlowHandler`` does: its import pulls in Alexa, Google Assistant
and the stream component.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant, callback

from .const import CONF_CLOUDHOOK_URL
from .runtime import RawConfigEntry

_LOGGER = logging.getLogger(__name__)
_LOCK = asyncio.Lock()


def _loaded(hass: HomeAssistant) -> bool:
    return "cloud" in hass.config.components


async def async_setup(hass: HomeAssistant, entry: RawConfigEntry) -> None:
    """Keep the entry's cloudhook URL current until it unloads."""
    if not _loaded(hass):
        return
    from homeassistant.components import cloud  # noqa: PLC0415

    webhook_id = entry.data[CONF_WEBHOOK_ID]

    @callback
    def forget() -> None:
        if CONF_CLOUDHOOK_URL in entry.data:
            data = {k: v for k, v in entry.data.items() if k != CONF_CLOUDHOOK_URL}
            hass.config_entries.async_update_entry(entry, data=data)

    @callback
    def changed(cloudhook: dict[str, Any] | None) -> None:
        if not cloudhook:
            forget()
        elif entry.data.get(CONF_CLOUDHOOK_URL) != cloudhook[CONF_CLOUDHOOK_URL]:
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_CLOUDHOOK_URL: cloudhook[CONF_CLOUDHOOK_URL]},
            )

    async def connection(state: Any) -> None:
        if state is cloud.CloudConnectionState.CLOUD_CONNECTED:
            await _async_create(hass, entry)
        elif not cloud.async_is_logged_in(hass):
            forget()

    entry.async_on_unload(
        cloud.async_listen_cloudhook_change(hass, webhook_id, changed)
    )
    if not cloud.async_is_logged_in(hass):
        forget()
    elif cloud.async_is_connected(hass):
        # In the background: a slow Nabu Casa API must never hold or fail setup.
        entry.async_create_background_task(
            hass, _async_create(hass, entry), "getraw cloudhook"
        )
    entry.async_on_unload(cloud.async_listen_connection_change(hass, connection))


async def _async_create(hass: HomeAssistant, entry: RawConfigEntry) -> None:
    from homeassistant.components import cloud  # noqa: PLC0415

    if CONF_CLOUDHOOK_URL in entry.data or not cloud.async_active_subscription(hass):
        return
    async with _LOCK:
        try:
            url = await cloud.async_get_or_create_cloudhook(
                hass, entry.data[CONF_WEBHOOK_ID]
            )
        except Exception as err:  # noqa: BLE001
            # Any cloud failure, a bare TimeoutError from hass_nabucasa's own
            # timeout included: the local and external routes
            # still work and the next connection retries. Only the type is
            # logged, never a URL.
            _LOGGER.warning("Home Assistant Cloud route not set up: %s", type(err).__name__)
            return
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CLOUDHOOK_URL: url}
    )


async def async_delete(hass: HomeAssistant, data: Any) -> None:
    """Delete the cloudhook of a webhook id that is going away."""
    if CONF_CLOUDHOOK_URL not in data or not _loaded(hass):
        return
    from homeassistant.components import cloud  # noqa: PLC0415

    try:
        await cloud.async_delete_cloudhook(hass, data[CONF_WEBHOOK_ID])
    except Exception as err:  # noqa: BLE001
        # A re-pair or a removal never fails for the cloud; the old cloudhook
        # then points at a webhook id nothing listens on.
        _LOGGER.warning("Home Assistant Cloud route not deleted: %s", type(err).__name__)
