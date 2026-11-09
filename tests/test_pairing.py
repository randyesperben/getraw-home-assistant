"""Pairing: the code's hook, Continue before the phone, pairing again, a fresh code and key."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from aiohttp.test_utils import TestClient

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, ConfigEntry
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.getraw.const import CONF_KEY, CONF_NEXT_KEY, DOMAIN

from .phone import ACCOUNT, OTHER_ACCOUNT, Phone, runtime_of


async def _start(hass: HomeAssistant, client: TestClient, **kwargs) -> tuple[dict, Phone]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return result, Phone.from_link(client, result["description_placeholders"]["link"], **kwargs)


async def reconfigure(hass: HomeAssistant, entry: ConfigEntry, step: str) -> dict:
    menu = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert menu["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        menu["flow_id"], {"next_step_id": step}
    )


async def pair(
    hass: HomeAssistant, client: TestClient, **phone: Any
) -> tuple[Phone, ConfigEntry]:
    """Scan, Connect, Continue, then the phone's next hello takes the entry's key."""
    result, phone = await _start(hass, client, **phone)
    assert (await phone.post(phone.hello())).ack["pending"] is True
    done = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    answer = await phone.post(phone.hello())
    phone.key = answer.ack["key"]
    return phone, done["result"]


async def test_the_entry_answers_on_the_codes_hook_after_the_flow_ends(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """The flow's cleanup runs after the entry's setup and must leave its handler."""
    result, phone = await _start(hass, raw_client)
    code_key = phone.key

    pending = await phone.post(phone.hello())
    assert pending.status == 200
    assert pending.ack["ok"] is True and pending.ack["pending"] is True
    assert "key" not in pending.ack

    done = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["title"] == "RAW Randy"
    assert done["result"].unique_id == phone.account_ref
    assert done["result"].data[CONF_WEBHOOK_ID] == phone.webhook_id

    first = await phone.post(phone.hello())
    assert first.ack["ok"] is True and "pending" not in first.ack
    new_key = first.ack["key"]
    assert new_key != code_key

    # Under the new key: confirmed, so no key in the ack and the code's key is dead.
    confirmed = await phone.post(phone.state(), key=new_key)
    assert confirmed.ack["ok"] is True and "key" not in confirmed.ack
    assert done["result"].data[CONF_KEY] == new_key
    assert CONF_NEXT_KEY not in done["result"].data
    assert (await phone.post(phone.state(), key=code_key)).status == 400


async def test_continue_before_the_phone_waits_for_its_hello(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    result, phone = await _start(hass, raw_client)
    waiting = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert waiting["type"] is FlowResultType.SHOW_PROGRESS

    assert (await phone.post(phone.hello())).ack["pending"] is True
    await hass.async_block_till_done()
    # The dialog moves on by itself (the frontend configures again after progress_done).
    done = await hass.config_entries.flow.async_configure(waiting["flow_id"])
    assert done["type"] is FlowResultType.CREATE_ENTRY

    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.unique_id == phone.account_ref
    assert "key" in (await phone.post(phone.hello())).ack


async def test_no_answer_shows_the_code_again_with_what_to_check(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    with patch("custom_components.getraw.config_flow.WAIT") as wait:
        wait.total_seconds.return_value = 0.01
        result, phone = await _start(hass, raw_client)
        await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    (flow,) = hass.config_entries.flow.async_progress()
    assert flow["step_id"] == "user"
    again = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert again["errors"] == {"base": "no_answer"}
    # The same code still works: the phone may only have been slow.
    assert (await phone.post(phone.hello())).ack["pending"] is True


async def test_closing_the_dialog_stops_the_codes_hook(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    result, phone = await _start(hass, raw_client)
    hass.config_entries.flow.async_abort(result["flow_id"])
    # Home Assistant's bare 200 for an unknown hook: no ack, so RAW says it.
    answer = await phone.post(phone.hello())
    assert (answer.status, answer.ack) == (200, None)


async def test_a_known_account_pairs_again_into_its_entry(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """A new phone or a reinstall keeps the device; its clock may run behind."""
    old_phone, entry = await pair(hass, raw_client)
    old_phone.revision += 10_000_000
    assert (await old_phone.post(old_phone.state("active"))).ack["ok"] is True

    result, phone = await _start(hass, raw_client)
    await phone.post(phone.hello())
    again = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "reconnected"
    await hass.async_block_till_done()

    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.data[CONF_WEBHOOK_ID] == phone.webhook_id
    assert (await old_phone.post(old_phone.state())).ack is None
    phone.key = (await phone.post(phone.hello())).ack["key"]
    # Below the old phone's revisions, applied all the same: a new pairing starts clean.
    assert (await phone.post(phone.state("resting"))).ack["ok"] is True
    assert runtime_of(hass).live == {"phase": "resting"}


async def test_show_the_code_again_answers_only_its_own_account(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """A fresh key on its own hook, never the one in use; another account's scan is refused."""
    _, entry = await pair(hass, raw_client)
    shown = await reconfigure(hass, entry, "show_code")
    assert shown["step_id"] == "show_code"
    link = shown["description_placeholders"]["link"]

    # Another account scans while the dialog waits: it says so at once, never
    # after the 2-minute wait as «RAW didn't answer».
    waiting = await hass.config_entries.flow.async_configure(shown["flow_id"], {})
    assert waiting["type"] is FlowResultType.SHOW_PROGRESS
    stranger = Phone.from_link(raw_client, link, account_ref=OTHER_ACCOUNT)
    assert (await stranger.post(stranger.hello())).ack["reason"] == "wrong_account"
    await hass.async_block_till_done()
    again = await hass.config_entries.flow.async_configure(shown["flow_id"])
    assert again["errors"] == {"base": "other_account"}
    # And when it scanned before Continue.
    assert (await stranger.post(stranger.hello())).ack["reason"] == "wrong_account"
    again = await hass.config_entries.flow.async_configure(shown["flow_id"], {})
    assert again["errors"] == {"base": "other_account"}

    phone = Phone.from_link(raw_client, link)
    assert (await phone.post(phone.hello())).ack["pending"] is True
    done = await hass.config_entries.flow.async_configure(shown["flow_id"], {})
    assert done["reason"] == "reconnected"


async def test_rotate_key_offers_a_fresh_key_in_the_next_ack(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    phone, entry = await pair(hass, raw_client)
    assert "key" not in (await phone.post(phone.state())).ack

    done = await reconfigure(hass, entry, "rotate_key")
    assert done["reason"] == "key_rotated"

    offered = (await phone.post(phone.state())).ack["key"]
    assert offered != phone.key
    assert "key" not in (await phone.post(phone.state(), key=offered)).ack


async def test_the_first_hello_keeps_the_code_for_its_account(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """Someone who saw the code cannot take the pairing before Continue."""
    result, phone = await _start(hass, raw_client)
    assert (await phone.post(phone.state())).ack["reason"] == "invalid"  # only a hello
    assert (await phone.post(phone.hello())).ack["pending"] is True

    stranger = Phone(phone.client, phone.webhook_id, phone.path, phone.key, account_ref=OTHER_ACCOUNT)
    stranger.revision = phone.revision + 1_000
    hello = stranger.hello()
    hello["display_name"] = "Mallory"
    assert (await stranger.post(hello)).ack["reason"] == "wrong_account"

    done = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["result"].unique_id == phone.account_ref
    assert done["result"].title == "RAW Randy"


async def test_a_name_given_to_the_entry_stays(
    hass: HomeAssistant, raw_client: TestClient
) -> None:
    """The phone renames the device and only the title RAW made."""
    phone, entry = await pair(hass, raw_client)
    hass.config_entries.async_update_entry(entry, title="Gym phone")

    hello = phone.hello()
    hello["display_name"] = "Ana"
    await phone.post(hello)
    assert entry.title == "Gym phone"
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, ACCOUNT), entry.entry_id)
    assert device is not None and device.name == "RAW Ana"

    shown = await reconfigure(hass, entry, "show_code")
    again = Phone.from_link(raw_client, shown["description_placeholders"]["link"])
    await again.post(again.hello())
    done = await hass.config_entries.flow.async_configure(shown["flow_id"], {})
    assert done["reason"] == "reconnected"
    assert entry.title == "Gym phone"
