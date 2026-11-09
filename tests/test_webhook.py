"""The entry's webhook: replays, refusals, repairs, removal (contracts 1 to 3)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
import json
from typing import Any

from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.getraw import webhook as webhook_module
from custom_components.getraw.const import CONF_KEY, DOMAIN
from custom_components.getraw.messages import MAX_BODY_BYTES, Event
from custom_components.getraw.runtime import signal_events
from custom_components.getraw.seal import Direction, decode_key, seal

from .phone import OTHER_ACCOUNT, Phone, ago, event_of, instant, runtime_of
from .test_pairing import pair


def _fired(hass: HomeAssistant, entry_id: str) -> list[str]:
    fired: list[str] = []

    # A callback runs in the event loop; a plain function would run in the
    # executor and land after the assertion under load.
    @callback
    def collect(events: list[Event]) -> None:
        fired.extend(event.event_id for event in events)

    async_dispatcher_connect(hass, signal_events(entry_id), collect)
    return fired


async def test_each_sequence_applies_only_newer_revisions(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, _ = await pair(hass, raw_client)
    await phone.post(phone.state("active"))
    stale = phone.state("resting")
    stale["revision"] -= 10
    answer = await phone.post(stale)
    # Delivered (the phone drops it) but older than what HA shows.
    assert answer.ack["ok"] is True
    assert runtime_of(hass).live == {"phase": "active"}

    summary = phone.body("summary", time_zone="Europe/Madrid")
    summary["revision"] = 5
    assert (await phone.post(summary)).ack["ok"] is True
    assert runtime_of(hass).summary["time_zone"] == "Europe/Madrid"


async def test_an_event_fires_once_and_only_while_fresh(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, entry = await pair(hass, raw_client)
    fired = _fired(hass, entry.entry_id)
    first = event_of("set_completed", ago(1))
    late = event_of("rest_started", ago(180))

    await phone.post(phone.event(first, late))
    await phone.post(phone.event(first))  # a retry: new revision, same id
    assert fired == [first["event_id"]]

    # An older revision never hides its events: they are filtered by id, not revision.
    behind = phone.event(event_of("record", ago(2)))
    behind["revision"] -= 1_000
    await phone.post(behind)
    assert len(fired) == 2


async def test_the_replay_guard_survives_a_reload(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, entry = await pair(hass, raw_client)
    seen = event_of("set_completed", ago(1))
    replayed = phone.event(seen)
    await phone.post(replayed)
    await phone.post(phone.state("resting"))

    assert await hass.config_entries.async_reload(entry.entry_id)
    fired = _fired(hass, entry.entry_id)
    # The same sealed message, captured and sent again: the reloaded entry
    # answers it (AUDIT-L2-2 M6) and applies nothing.
    assert (await phone.post(replayed)).ack["ok"] is True
    assert fired == []
    assert runtime_of(hass).live == {"phase": "resting"}


async def test_refusals_tell_the_phone_why(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, entry = await pair(hass, raw_client)
    issues = ir.async_get(hass)
    issue = (DOMAIN, f"unsupported_version_{entry.entry_id}")

    # An older phone: RAW on the phone needs the update, never the integration
    # (AUDIT-L2-2 M14).
    older = phone.state()
    older["schema_version"] = 0
    assert (await phone.post(older)).ack["reason"] == "unsupported_version"
    assert issues.async_get_issue(*issue) is None

    newer = phone.state()
    newer["schema_version"] = 2
    assert (await phone.post(newer)).ack["reason"] == "unsupported_version"
    assert issues.async_get_issue(*issue) is not None
    assert runtime_of(hass).refused_version is True
    # A replayed older message clears nothing and is not news (AUDIT-L2-2 M3).
    heard = runtime_of(hass).guard.heard_at
    replay = phone.state()
    replay["revision"] -= 1_000
    assert (await phone.post(replay)).ack["ok"] is True
    assert issues.async_get_issue(*issue) is not None
    assert runtime_of(hass).guard.heard_at == heard
    await phone.post(phone.state())
    assert issues.async_get_issue(*issue) is None

    stranger = phone.state()
    stranger["account_ref"] = OTHER_ACCOUNT
    assert (await phone.post(stranger)).ack["reason"] == "wrong_account"

    broken = phone.body("bye", reason="lost")
    assert (await phone.post(broken)).ack["reason"] == "invalid"


async def test_a_body_that_does_not_open_gets_no_ack(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, _ = await pair(hass, raw_client)
    reflected = seal(
        decode_key(phone.key), Direction.TO_PHONE, phone.webhook_id, b'{"kind":"state"}'
    )
    for body in (
        json.dumps({"sealed": reflected}).encode(),  # an ack sent back as a post
        json.dumps({"sealed": 1}).encode(),
        b"not json",
    ):
        answer = await phone.post(body)
        assert (answer.status, answer.ack) == (400, None)
    oversized = await phone.post(b"x" * (MAX_BODY_BYTES + 1))
    assert (oversized.status, oversized.ack) == (413, None)
    # A revision past what every reader holds exactly would freeze its sequence
    # until a re-pair (AUDIT-L2-2 M2).
    huge = phone.state()
    huge["revision"] = 2**53
    answer = await phone.post(huge)
    assert (answer.status, answer.ack) == (400, None)


async def test_a_removed_entry_tells_the_phone_sealed(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, entry = await pair(hass, raw_client)
    assert await hass.config_entries.async_remove(entry.entry_id)
    answer = await phone.post(phone.state())
    assert answer.status == 410
    assert answer.ack["reason"] == "removed"


async def test_bye_and_silence_raise_repairs_except_after_a_pause(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    phone, entry = await pair(hass, raw_client)
    issues = ir.async_get(hass)
    gone = (DOMAIN, f"phone_disconnected_{entry.entry_id}")
    silent = (DOMAIN, f"silent_{entry.entry_id}")

    await phone.post(phone.body("bye", reason="signed_out"))
    assert issues.async_get_issue(*gone) is not None
    await phone.post(phone.state())
    assert issues.async_get_issue(*gone) is None

    await phone.post(phone.body("bye", reason="paused"))
    freezer.tick(timedelta(days=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert issues.async_get_issue(*gone) is None
    assert issues.async_get_issue(*silent) is None

    await phone.post(phone.state())
    freezer.tick(timedelta(days=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert issues.async_get_issue(*silent) is not None
    await phone.post(phone.state())
    assert issues.async_get_issue(*silent) is None


async def test_a_held_back_message_never_makes_its_events_young(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """AUDIT-L2-2 I1: HA never raises the offset, so holding a post only ages its events."""
    phone, entry = await pair(hass, raw_client)
    fired = _fired(hass, entry.entry_id)
    await phone.post(phone.state("active"))

    held = phone.event(event_of("workout_started", ago(3600)))
    held["phone_now"] = instant(ago(3600))
    assert (await phone.post(held)).ack["ok"] is True
    assert fired == []


async def test_a_phone_clock_behind_still_fires_on_time(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, entry = await pair(hass, raw_client, behind=timedelta(minutes=10))
    fired = _fired(hass, entry.entry_id)
    await phone.post(phone.state("active"))

    await phone.post(phone.event(event_of("set_completed", ago(600))))
    assert len(fired) == 1


async def test_a_replay_never_fires_again_inside_or_after_its_window(
    hass: HomeAssistant, raw_client: TestClient, freezer: FrozenDateTimeFactory
) -> None:
    phone, entry = await pair(hass, raw_client)
    fired = _fired(hass, entry.entry_id)
    message = phone.event(event_of("set_completed", ago(1)))
    await phone.post(message)

    freezer.tick(timedelta(seconds=90))  # its id still remembered
    await phone.post(message)
    freezer.tick(timedelta(minutes=10))  # its id forgotten, its age still counts
    await phone.post(message)
    assert len(fired) == 1


async def test_the_guard_is_on_disk_two_seconds_after_a_message(
    hass: HomeAssistant,
    raw_client: TestClient,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """A crash after that loses nothing a replay could use (the plan audit's A-M5)."""
    phone, entry = await pair(hass, raw_client)
    state = phone.state("active")
    await phone.post(state)

    freezer.tick(timedelta(seconds=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    saved = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert saved["revisions"]["live"] == state["revision"]


async def test_an_older_bye_leaves_the_phone_connected(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, entry = await pair(hass, raw_client)
    late = phone.body("bye", reason="signed_out")
    await phone.post(phone.state("active"))
    assert (await phone.post(late)).ack["ok"] is True

    assert runtime_of(hass).guard.bye is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"phone_disconnected_{entry.entry_id}") is None


def _held_post(
    client: TestClient, phone: Phone, body: dict[str, Any], key: str
) -> tuple[asyncio.Future[Any], asyncio.Event]:
    """A post whose body stops after 8 bytes until the gate opens."""
    sealed = seal(
        decode_key(key), Direction.TO_HOME_ASSISTANT, phone.webhook_id, json.dumps(body).encode()
    )
    payload = json.dumps({"sealed": sealed}).encode()
    gate = asyncio.Event()

    async def slowly() -> AsyncIterator[bytes]:
        yield payload[:8]
        await gate.wait()
        yield payload[8:]

    return asyncio.ensure_future(client.post(phone.path, data=slowly())), gate


async def test_a_post_held_across_a_key_promotion_cannot_use_the_old_key(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """AUDIT-L2-2 I3: the keys are read once the body is in."""
    phone, entry = await pair(hass, raw_client)
    held, gate = _held_post(raw_client, phone, phone.state("finishing"), entry.data[CONF_KEY])
    await asyncio.sleep(0.05)
    assert (await phone.post(phone.state("active"))).ack["ok"] is True  # promotes the offered key
    gate.set()

    assert (await held).status == 400
    assert runtime_of(hass).live == {"phase": "active"}


async def test_a_post_held_across_an_unload_gets_no_answer(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """Its body lands after the entry stopped: no ack, nothing written for a guard that is gone."""
    phone, entry = await pair(hass, raw_client)
    held, gate = _held_post(raw_client, phone, phone.state("finishing"), phone.key)
    await asyncio.sleep(0.05)
    assert await hass.config_entries.async_unload(entry.entry_id)
    gate.set()

    response = await held
    assert response.status == 503
    assert await response.read() == b""


async def test_clocks_moved_apart_raise_a_repair_and_keep_the_offset(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """AUDIT-L2-3 M1: NTP fixes a phone that ran 5 minutes ahead; the offset stays (I1)."""
    phone, entry = await pair(hass, raw_client, behind=timedelta(minutes=-5))
    await phone.post(phone.state("active"))
    offset = runtime_of(hass).guard.offset_ms
    issues = ir.async_get(hass)
    clock = (DOMAIN, f"clock_{entry.entry_id}")

    phone.behind = timedelta(0)
    for _ in range(2):  # one post held on the way says nothing yet
        await phone.post(phone.state("active"))
    assert issues.async_get_issue(*clock) is None
    await phone.post(phone.state("active"))
    assert issues.async_get_issue(*clock) is not None
    assert runtime_of(hass).guard.offset_ms == offset

    phone.behind = timedelta(minutes=-5)  # the clocks agree again
    await phone.post(phone.state("active"))
    assert issues.async_get_issue(*clock) is None


async def test_a_summary_held_on_its_zone_cannot_outlive_its_key(
    hass: HomeAssistant, raw_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUDIT-L2-3 M2: the key is checked again once the summary's zone is read."""
    phone, entry = await pair(hass, raw_client)
    entered, gate = asyncio.Event(), asyncio.Event()
    read_zone = webhook_module.zone_of

    async def held_zone(summary: dict[str, Any]) -> Any:
        entered.set()
        await gate.wait()
        return await read_zone(summary)

    monkeypatch.setattr(webhook_module, "zone_of", held_zone)
    summary = phone.body("summary", time_zone="Europe/Madrid")
    held = asyncio.ensure_future(phone.post(summary, key=entry.data[CONF_KEY]))
    await entered.wait()
    assert (await phone.post(phone.state("active"))).ack["ok"] is True  # promotes the offered key
    gate.set()

    assert (await held).status == 400
    assert runtime_of(hass).summary is None
