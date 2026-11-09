"""The entry's webhook: open, check, apply, answer.

Home Assistant answers 200 to an unknown hook and to a handler that raises, so
the phone counts a post delivered only when this handler answers with an ack
sealed under the key the post came under. A body that does not open gets 400
and no ack; one that opens but is refused gets ``ok: false`` and its reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
import json
from typing import Any, Final

from aiohttp import web
from aiohttp.hdrs import METH_POST

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CONF_APP_VERSION,
    CONF_CLOUDHOOK_URL,
    CONF_DISPLAY_NAME,
    CONF_KEY,
    CONF_NEXT_KEY,
    CONF_PLATFORM,
    DOMAIN,
    PHONE_EVENT_TYPES,
)
from .entity import entry_title
from .messages import (
    READS,
    Message,
    Refused,
    ack,
    check_envelope,
    events_of,
    parse_message,
    read_body,
    sealed_value,
)
from .routes import async_certificate_pin, routes
from .runtime import RawConfigEntry, signal_update, zone_of
from .seal import Direction, SealError, decode_key, open_sealed, seal

LEARN_MORE: Final = "https://getraw.fit/home-assistant"
ISSUE_UNSUPPORTED_VERSION: Final = "unsupported_version"
ISSUE_PHONE_DISCONNECTED: Final = "phone_disconnected"
ISSUE_SILENT: Final = "silent"
ISSUE_CLOCK: Final = "clock"


def issue_id(kind: str, entry_id: str) -> str:
    return f"{kind}_{entry_id}"


@callback
def async_raise_issue(hass: HomeAssistant, entry: RawConfigEntry, kind: str) -> None:
    """Raise a repair the person reads; Home Assistant deletes it with the entry."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(kind, entry.entry_id),
        config_entry_id=entry.entry_id,
        is_fixable=False,
        learn_more_url=LEARN_MORE,
        severity=ir.IssueSeverity.WARNING,
        translation_key=kind,
        translation_placeholders={"name": entry.title},
    )


def title_for(entry: RawConfigEntry, display_name: str | None) -> str:
    """The entry's title for a phone named ``display_name``.

    Only the title RAW made follows the phone's name; one the person gave the
    entry stays.
    """
    if entry.title != entry_title(entry.data.get(CONF_DISPLAY_NAME)):
        return entry.title
    return entry_title(display_name)


def bad_request() -> web.Response:
    """A body that does not open: no ack, so the phone tries its next route."""
    return web.Response(status=400)


def open_with(
    keys: Sequence[str], webhook_id: str, sealed: str
) -> tuple[bytes, str] | None:
    """The plaintext and the key it opened under, trying each key in turn."""
    for key in keys:
        try:
            return open_sealed(decode_key(key), Direction.TO_HOME_ASSISTANT, webhook_id, sealed), key
        except SealError:
            continue
    return None


def answer(
    key: str, webhook_id: str, body: dict[str, Any], *, status: int = 200
) -> web.Response:
    """``body`` sealed for the phone under ``key``."""
    plaintext = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    sealed = seal(decode_key(key), Direction.TO_PHONE, webhook_id, plaintext)
    return web.json_response({"sealed": sealed}, status=status)


async def read_message(
    request: web.Request, keys: Sequence[str], webhook_id: str
) -> tuple[Message, str] | web.Response:
    """The opened message and its key, or the response for a body that is not one."""
    body = await read_body(request)
    if body is None:
        return web.Response(status=413)
    return open_message(body, keys, webhook_id)


def open_message(
    body: bytes, keys: Sequence[str], webhook_id: str
) -> tuple[Message, str] | web.Response:
    """``read_message`` once the body is in: no wait, so the keys are current."""
    sealed = sealed_value(body)
    opened = open_with(keys, webhook_id, sealed) if sealed is not None else None
    if opened is None:
        return bad_request()
    plaintext, key = opened
    message = parse_message(plaintext)
    if message is None:
        return bad_request()
    return message, key


def _keys(data: Any) -> list[str]:
    return [k for k in (data.get(CONF_NEXT_KEY), data.get(CONF_KEY)) if k]


@callback
def async_register(hass: HomeAssistant, entry: RawConfigEntry) -> None:
    """Listen on the entry's webhook until it unloads."""
    webhook_id = entry.runtime_data.webhook_id
    webhook.async_register(
        hass,
        DOMAIN,
        entry.title,
        webhook_id,
        partial(_async_handle, entry),
        allowed_methods=[METH_POST],
    )
    entry.async_on_unload(partial(webhook.async_unregister, hass, webhook_id))


async def _async_handle(
    entry: RawConfigEntry, hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.Response:
    # The body first: the keys and the runtime are read only once it is in, so
    # a post that began before a key's promotion or a reload cannot use the old
    # ones.
    body = await read_body(request)
    if body is None:
        return web.Response(status=413)
    if entry.state is not ConfigEntryState.LOADED:
        return web.Response(status=503)
    read = open_message(body, _keys(entry.data), webhook_id)
    if isinstance(read, web.Response):
        return read
    message, key = read
    received = dt_util.utcnow()
    try:
        phone_now = check_envelope(message, entry.unique_id)
    except Refused as refused:
        if refused.reason == ISSUE_UNSUPPORTED_VERSION:
            _refuse_version(hass, entry, message)
        return answer(key, webhook_id, ack(message, reason=refused.reason))

    # The summary's zone may need the disk; read it before anything is applied,
    # so two posts never apply out of order around the wait.
    zone = None
    if message.kind == "summary":
        zone = await zone_of(message.body)
        if entry.state is not ConfigEntryState.LOADED or key not in _keys(entry.data):
            return bad_request()
    runtime = entry.runtime_data
    offered = _confirm_key(hass, entry, key)

    body = message.body
    bye = body["reason"] if message.kind == "bye" else None
    newer = runtime.guard.heard(
        sequence=message.sequence,
        revision=message.revision,
        phone_now=phone_now,
        received=received,
        bye=bye,
    )
    events = events_of(message) if message.kind in ("event", "patch") else []
    # Only the types this release names: a newer phone's are ignored, and the
    # two Home Assistant fires itself never come from the phone.
    known = [event for event in events if event.type in PHONE_EVENT_TYPES]
    fresh = runtime.guard.fresh(known, received) if known else []
    if newer:
        _phone_said(hass, entry, message)
    runtime.apply(
        message.kind, body if newer else None, events=events, fire=fresh, zone=zone
    )

    urls = routes(
        hass,
        webhook_id,
        pin=await async_certificate_pin(hass),
        cloudhook_url=entry.data.get(CONF_CLOUDHOOK_URL),
    )
    return answer(
        key, webhook_id, ack(message, urls=[r.as_ack() for r in urls], key=offered)
    )


@callback
def _confirm_key(hass: HomeAssistant, entry: RawConfigEntry, used: str) -> str | None:
    """Promote the offered key once a post comes under it; else the key still to offer."""
    offered = entry.data.get(CONF_NEXT_KEY)
    if offered is None:
        return None
    if used != offered:
        return offered
    data = {k: v for k, v in entry.data.items() if k != CONF_NEXT_KEY}
    hass.config_entries.async_update_entry(entry, data={**data, CONF_KEY: offered})
    return None


@callback
def _refuse_version(hass: HomeAssistant, entry: RawConfigEntry, message: Message) -> None:
    """Show the entities unavailable while the phone writes a version this release cannot read.

    The repair asks to update the integration only when the phone is the newer
    one; an older phone learns from the ack's ``reads`` that
    RAW on the phone needs the update.
    """
    entry.runtime_data.refused_version = True
    if message.body["schema_version"] > max(READS[message.kind]):
        async_raise_issue(hass, entry, ISSUE_UNSUPPORTED_VERSION)
    async_dispatcher_send(hass, signal_update(entry.entry_id))


@callback
def _phone_said(hass: HomeAssistant, entry: RawConfigEntry, message: Message) -> None:
    """What a newer message changes beyond the entities: the repairs, the phone's name.

    Only a newer one: a replay of an old message clears no repair.
    """
    runtime = entry.runtime_data
    if runtime.refused_version:
        runtime.refused_version = False
        ir.async_delete_issue(hass, DOMAIN, issue_id(ISSUE_UNSUPPORTED_VERSION, entry.entry_id))
    ir.async_delete_issue(hass, DOMAIN, issue_id(ISSUE_SILENT, entry.entry_id))
    if runtime.guard.clocks_apart:
        async_raise_issue(hass, entry, ISSUE_CLOCK)
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id(ISSUE_CLOCK, entry.entry_id))
    if message.kind == "bye":
        if message.body["reason"] != "paused":
            async_raise_issue(hass, entry, ISSUE_PHONE_DISCONNECTED)
        return
    ir.async_delete_issue(hass, DOMAIN, issue_id(ISSUE_PHONE_DISCONNECTED, entry.entry_id))
    if message.kind == "hello":
        async_update_phone(hass, entry, message.body)


@callback
def async_update_phone(
    hass: HomeAssistant, entry: RawConfigEntry, hello: dict[str, Any]
) -> None:
    """The entry's title and phone details from a ``hello``; a profile without a name drops it."""
    said = phone_data(hello)
    data = {k: v for k, v in entry.data.items() if k != CONF_DISPLAY_NAME} | said
    name = data.get(CONF_DISPLAY_NAME)
    hass.config_entries.async_update_entry(entry, title=title_for(entry, name), data=data)
    # The device keeps the name it was created with until told; a name the
    # person gave it (name_by_user) and its entity ids stay as they were.
    devices = dr.async_get(hass)
    if device := devices.async_get_device_by_identifier(
        (DOMAIN, entry.unique_id or entry.entry_id), entry.entry_id
    ):
        devices.async_update_device(
            device.id, name=entry_title(name), sw_version=data.get(CONF_APP_VERSION)
        )


def phone_data(hello: dict[str, Any]) -> dict[str, str]:
    """What a ``hello`` says about the phone, as the entry keeps it."""
    data: dict[str, str] = {}
    if isinstance(name := hello.get("display_name"), str) and name.strip():
        data[CONF_DISPLAY_NAME] = name.strip()[:100]
    app = hello.get("app")
    if isinstance(app, dict) and isinstance(version := app.get("version"), str):
        data[CONF_APP_VERSION] = version[:20]
    if hello.get("platform") in ("android", "ios"):
        data[CONF_PLATFORM] = hello["platform"]
    return data


@callback
def async_register_tombstone(hass: HomeAssistant, data: Any) -> None:
    """Answer a removed entry's hook with a sealed ``removed`` and HTTP 410.

    Sealed, so a captive portal or another network's host at the same address
    can never make the phone forget its pairing. Kept while the integration is
    loaded, as mobile_app's deleted ids are; the keys live only in memory.
    """
    webhook_id = data[CONF_WEBHOOK_ID]
    keys = _keys(data)

    async def handle(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        read = await read_message(request, keys, webhook_id)
        if isinstance(read, web.Response):
            return read
        message, key = read
        return answer(key, webhook_id, ack(message, reason="removed"), status=410)

    webhook.async_unregister(hass, webhook_id)
    webhook.async_register(
        hass, DOMAIN, "RAW (removed)", webhook_id, handle, allowed_methods=[METH_POST]
    )
