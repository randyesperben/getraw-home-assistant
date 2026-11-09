"""Pairing: a QR code in Home Assistant, Connect in RAW (plan «Pairing», ADR 0173).

One entry per RAW account (its ``account_ref`` is the unique id), so a new phone,
a reinstall or «Show the QR code again» re-pairs into the same device, entity ids
and history. Reconfigure also offers «Rotate key», which offers a fresh key in
the next ack.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Final

import probatio

from homeassistant.components import webhook
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import callback
from homeassistant.helpers import instance_id, selector

from . import cloudhook
from .const import CONF_DISPLAY_NAME, CONF_KEY, CONF_NEXT_KEY, DOMAIN
from .pairing import PairingHook
from .pairing_link import build_link
from .routes import LOCAL, async_certificate_pin, routes
from .seal import encode_key, new_key
from .entity import entry_title
from .webhook import phone_data, title_for

# How long the progress step waits for the phone after Continue.
WAIT: Final = timedelta(minutes=2)


class RawConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair a phone, or pair it again into its account's entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._hook: PairingHook | None = None
        self._link = ""
        self._address = ""
        self._code_step = "user"
        self._waiting: asyncio.Task[None] | None = None
        self._error: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The code to scan, then Continue."""
        return await self._async_pair("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["show_code", "rotate_key"]
        )

    async def async_step_show_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A fresh code and key for this entry's account, as pairing does (F-M8)."""
        return await self._async_pair("show_code", user_input)

    async def async_step_rotate_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The next ack offers a fresh key; the phone switches at its next post."""
        return self.async_update_and_abort(
            self._get_reconfigure_entry(),
            data_updates={CONF_NEXT_KEY: encode_key(new_key())},
            reason="key_rotated",
        )

    async def _async_pair(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        if self._hook is None and (refused := await self._async_start(step_id)):
            return refused
        assert self._hook is not None
        if user_input is None:
            return self._show_code()
        if self._hook.hello is not None:
            return await self.async_step_finish()
        if self._hook.take_other_account():
            # Scanned by another account before Continue: say so now (AUDIT-L2-2 M13).
            self._error = "other_account"
            return self._show_code()
        self._waiting = self.hass.async_create_task(
            self._hook.async_wait(WAIT.total_seconds())
        )
        return await self.async_step_wait()

    async def _async_start(self, step_id: str) -> ConfigFlowResult | None:
        """Mint the webhook id and key, listen, build the link; None when it is shown."""
        hass = self.hass
        account_ref = (
            self._get_reconfigure_entry().unique_id
            if self.source == SOURCE_RECONFIGURE
            else None
        )
        webhook_id = webhook.async_generate_id()
        pin = await async_certificate_pin(hass)
        found = routes(hass, webhook_id, pin=pin, cloudhook_url=None)
        if not found:
            return self.async_abort(reason="no_address")
        key = new_key()
        self._link = build_link(
            urls=[route.url for route in found],
            webhook_id=webhook_id,
            key=key,
            instance_id=await instance_id.async_get(hass),
            name=hass.config.location_name,
            pin=pin if any(route.pin for route in found) else None,
        )
        local = [route.url for route in found if route.route == LOCAL]
        self._address = (local or [found[0].url])[0].split("/api/webhook/")[0]
        self._code_step = step_id
        self._hook = PairingHook(
            hass, webhook_id=webhook_id, key=key, account_ref=account_ref, routes=found
        )
        self._hook.register()
        return None

    @callback
    def _show_code(self) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=self._code_step,
            data_schema=probatio.Schema(
                {
                    probatio.Optional("code"): selector.QrCodeSelector(
                        selector.QrCodeSelectorConfig(
                            data=self._link,
                            scale=4,
                            error_correction_level=selector.QrErrorCorrectionLevel.MEDIUM,
                        )
                    )
                }
            ),
            description_placeholders={"link": self._link, "address": self._address},
            errors={"base": self._error} if self._error else None,
            last_step=False,
        )

    async def async_step_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Up to two minutes for the phone, then the code again with what to check."""
        assert self._hook is not None
        if self._waiting is not None and not self._waiting.done():
            return self.async_show_progress(
                step_id="wait",
                progress_action="wait_for_phone",
                progress_task=self._waiting,
            )
        self._waiting = None
        if self._hook.hello is None:
            self._error = (
                "other_account" if self._hook.take_other_account() else "no_answer"
            )
            return self.async_show_progress_done(next_step_id=self._code_step)
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the account's entry, or pair it again into the one it has."""
        hook = self._hook
        assert hook is not None and hook.hello is not None
        account_ref = hook.hello["account_ref"]
        await self.async_set_unique_id(account_ref, raise_on_progress=False)
        data: dict[str, Any] = {
            CONF_WEBHOOK_ID: hook.webhook_id,
            CONF_KEY: encode_key(hook.key),
            # Offered in the first ack under the entry: the code's key stops at first use.
            CONF_NEXT_KEY: encode_key(new_key()),
            **phone_data(hook.hello),
        }
        # Before the entry's setup registers this same id (F-C1).
        hook.release()
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, account_ref
        )
        if entry is None:
            return self.async_create_entry(
                title=entry_title(data.get(CONF_DISPLAY_NAME)), data=data
            )
        # The old webhook id's cloudhook goes with it; setup makes one for the new id.
        await cloudhook.async_delete(self.hass, entry.data)
        return self.async_update_reload_and_abort(
            entry,
            title=title_for(entry, data.get(CONF_DISPLAY_NAME)),
            data=data,
            reason="reconnected",
        )

    @callback
    def async_remove(self) -> None:
        """A closed dialog: stop listening, unless the entry already took the id."""
        if self._hook is not None:
            self._hook.release()
