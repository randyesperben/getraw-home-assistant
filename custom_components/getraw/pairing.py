"""The temporary hook a pairing flow listens on.

The flow mints the entry's webhook id and the pairing key, and listens on that id
while its dialog is open: a ``hello`` under the key is kept and answered
``pending``, since the entry exists only once the person continues. The flow
releases the hook before it creates or updates the entry, whose own handler then
takes the same id; a closed dialog releases it too, and only while the flow still
owns it, because ``FlowHandler.async_remove`` runs after the new entry's setup.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from aiohttp import web
from aiohttp.hdrs import METH_POST

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .messages import Refused, ack, check_envelope
from .routes import Route
from .seal import encode_key
from .webhook import answer, read_message


class PairingHook:
    """One pairing code's hook, owned by the flow that showed the code."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        webhook_id: str,
        key: bytes,
        account_ref: str | None,
        routes: list[Route],
    ) -> None:
        self.hass = hass
        self.webhook_id = webhook_id
        self.key = key
        # A reconfigure's code answers only the entry's own account.
        self._account_ref = account_ref
        self._routes = routes
        self.hello: dict[str, Any] | None = None
        # A phone on another account scanned the code: the dialog says so at once.
        self._other_account = False
        self._answered = asyncio.Event()
        self._owned = False

    @callback
    def register(self) -> None:
        webhook.async_register(
            self.hass,
            DOMAIN,
            "RAW pairing",
            self.webhook_id,
            self._async_handle,
            allowed_methods=[METH_POST],
        )
        self._owned = True

    @callback
    def release(self) -> None:
        """Stop listening, once; after that the id may be the entry's."""
        if self._owned:
            webhook.async_unregister(self.hass, self.webhook_id)
            self._owned = False

    async def async_wait(self, seconds: float) -> None:
        """Return when a hello has come or another account was refused, or after ``seconds``."""
        with suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._answered.wait()

    @callback
    def take_other_account(self) -> bool:
        """Whether another account scanned the code since this was last asked."""
        refused, self._other_account = self._other_account, False
        if self.hello is None:
            self._answered.clear()
        return refused

    async def _async_handle(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        read = await read_message(request, [encode_key(self.key)], webhook_id)
        if isinstance(read, web.Response):
            return read
        message, key = read
        try:
            check_envelope(message, self._account_ref)
            if message.kind != "hello":
                raise Refused("invalid")
        except Refused as refused:
            if refused.reason == "wrong_account" and self.hello is None:
                self._other_account = True
                self._answered.set()
            return answer(key, webhook_id, ack(message, reason=refused.reason))
        # The first hello pins the code to its account: someone else who saw the
        # code gets wrong_account from then on.
        self._account_ref = message.body["account_ref"]
        if self.hello is None or message.revision >= self.hello["revision"]:
            self.hello = message.body
        self._answered.set()
        urls = [route.as_ack() for route in self._routes]
        return answer(key, webhook_id, ack(message, urls=urls, pending=True))
