"""The URLs in the code and in every ack: local, pinned, external and cloud."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from enum import Enum
from pathlib import Path
import ssl
import sys
import types
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID
import pytest

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.getraw import routes as routes_module
from custom_components.getraw.const import CONF_CLOUDHOOK_URL, DOMAIN
from custom_components.getraw.pairing_link import certificate_pin

from .conftest import FIXTURES
from .phone import Phone
from .test_pairing import _start, pair
from .test_pairing_link import read_link

WEBHOOK = "/api/webhook/"
CLOUDHOOK = "https://hooks.nabu.casa/fixture-cloudhook"


def _ip_route(hass: HomeAssistant, scheme: str) -> str:
    api = hass.config.api
    return f"{scheme}://{api.local_ip}:{api.port}"


def _resolves(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> list[str]:
    """Every name resolves to ``addresses`` on Home Assistant's machine; the names looked up."""
    looked_up: list[str] = []

    async def addresses_of(host: str) -> set[str]:
        looked_up.append(host)
        return set(addresses)

    monkeypatch.setattr(routes_module, "_addresses_of", addresses_of)
    return looked_up


async def _code_urls(hass: HomeAssistant, client: TestClient) -> list[str]:
    result, _ = await _start(hass, client)
    urls = read_link(result["description_placeholders"]["link"])["urls"]
    return [url.split(WEBHOOK)[0] for url in urls]


async def test_the_code_tries_the_internal_url_then_the_ip_address(
    hass: HomeAssistant, raw_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IP route follows an internal URL that names it: the name may not resolve on the phone."""
    looked_up = _resolves(monkeypatch, hass.config.api.local_ip)
    await async_process_ha_core_config(
        hass,
        {
            "internal_url": "http://homeassistant.local:8123",
            "external_url": "https://casa.duckdns.org",
        },
    )
    assert await _code_urls(hass, raw_client) == [
        "http://homeassistant.local:8123",
        _ip_route(hass, "http"),
        "https://casa.duckdns.org",
    ]
    # Every ack builds its routes: the name is looked up once in a while, not each time.
    await _code_urls(hass, raw_client)
    assert looked_up == ["homeassistant.local"]


async def test_plain_http_stays_on_the_local_network(
    hass: HomeAssistant, raw_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a public host over HTTP, never the loopback."""
    _resolves(monkeypatch, hass.config.api.local_ip)
    await async_process_ha_core_config(
        hass,
        {
            "internal_url": "http://homeassistant.fritz.box:8123",
            "external_url": "http://casa.duckdns.org:8123",
        },
    )
    assert await _code_urls(hass, raw_client) == [_ip_route(hass, "http")]

    # No network: Home Assistant's own address falls back to the loopback one.
    monkeypatch.setattr(hass.config.api, "local_ip", "127.0.0.1")
    await async_process_ha_core_config(hass, {"external_url": "http://1.2.3.4:8123"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "no_address")


async def test_a_container_without_host_networking_never_hands_out_its_own_address(
    hass: HomeAssistant, raw_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link carries the local address the person set, never one no phone reaches.

    Inside Docker's bridge network Home Assistant's own address is the
    container's (RAW's device pass: OrbStack's 192.168.215.2 beside the Mac's
    10.0.0.31), while the internal URL, an address or a name, reaches the host.
    """
    monkeypatch.setattr(hass.config.api, "local_ip", "192.168.215.2")
    _resolves(monkeypatch, "10.0.0.31")
    await async_process_ha_core_config(hass, {"internal_url": "http://10.0.0.31:8123"})
    assert await _code_urls(hass, raw_client) == ["http://10.0.0.31:8123"]

    await async_process_ha_core_config(
        hass, {"internal_url": "http://homeassistant.local:8123"}
    )
    assert await _code_urls(hass, raw_client) == ["http://homeassistant.local:8123"]


@pytest.fixture
def served_tls(tmp_path: Path) -> tuple[bytes, ssl.SSLContext]:
    """A certificate and a server context holding it, as Home Assistant loads at start."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "homeassistant.local")])
    now = dt_util.utcnow()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(Encoding.PEM)
    (tmp_path / "served.pem").write_bytes(pem)
    (tmp_path / "served.key").write_bytes(
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(tmp_path / "served.pem", tmp_path / "served.key")
    return pem, context


async def test_home_assistant_serving_tls_pins_the_certificate_it_serves(
    hass: HomeAssistant,
    raw_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    served_tls: tuple[bytes, ssl.SSLContext],
) -> None:
    """The pin lets the phone check a certificate issued for a name.

    It hashes what the server hands out: the file on disk may have been renewed
    since Home Assistant started and is not served until a restart.
    """
    served, context = served_tls

    async def close(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(close, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    renewed = FIXTURES / "pairing_link" / "pins" / "full_chain.pem"
    monkeypatch.setattr(hass.config.api, "use_ssl", True)
    monkeypatch.setattr(hass.config.api, "port", port)
    # Without raising=False: a rename in Home Assistant fails here.
    monkeypatch.setattr(hass.http, "server_host", ["0.0.0.0"])
    monkeypatch.setattr(hass.http, "ssl_certificate", str(renewed))
    _resolves(monkeypatch, hass.config.api.local_ip)
    await async_process_ha_core_config(
        hass, {"internal_url": "https://homeassistant.local:8123"}
    )
    try:
        phone, _ = await pair(hass, raw_client)
        urls = (await phone.post(phone.state())).ack["urls"]
    finally:
        server.close()
        await server.wait_closed()

    assert urls == [
        {"url": "https://homeassistant.local:8123" + phone.path, "route": "local"},
        {
            "url": _ip_route(hass, "https") + phone.path,
            "route": "local",
            "pin": certificate_pin(served),
        },
    ]


@pytest.fixture
def cloud(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Home Assistant Cloud, logged in and subscribed, as far as RAW asks it."""
    fake = types.ModuleType("homeassistant.components.cloud")
    fake.CloudNotAvailable = type("CloudNotAvailable", (Exception,), {})
    fake.CloudConnectionState = Enum(
        "CloudConnectionState", "CLOUD_CONNECTED CLOUD_DISCONNECTED"
    )
    fake.async_is_logged_in = lambda hass: True
    fake.async_is_connected = lambda hass: True
    fake.async_active_subscription = lambda hass: True
    fake.async_get_or_create_cloudhook = AsyncMock(return_value=CLOUDHOOK)
    fake.async_delete_cloudhook = AsyncMock()
    fake.async_listen_cloudhook_change = lambda hass, webhook_id, on_change: lambda: None
    fake.async_listen_connection_change = lambda hass, target: lambda: None
    monkeypatch.setitem(sys.modules, "homeassistant.components.cloud", fake)
    hass.config.components.add("cloud")
    return fake


async def test_the_cloud_route_reaches_the_phone_in_the_next_ack(
    hass: HomeAssistant, raw_client: TestClient, cloud: types.ModuleType
) -> None:
    """Pairing never waits for the cloud; the entry's setup adds it as mobile_app does."""
    phone, entry = await pair(hass, raw_client)
    assert entry.data[CONF_CLOUDHOOK_URL] == CLOUDHOOK
    cloud.async_get_or_create_cloudhook.assert_awaited_once_with(hass, phone.webhook_id)

    urls = (await phone.post(phone.state())).ack["urls"]
    assert urls[-1] == {"url": CLOUDHOOK, "route": "cloud"}

    assert await hass.config_entries.async_remove(entry.entry_id)
    cloud.async_delete_cloudhook.assert_awaited_once_with(hass, phone.webhook_id)


async def test_pairing_again_retires_the_old_cloudhook(
    hass: HomeAssistant, raw_client: TestClient, cloud: types.ModuleType
) -> None:
    old_phone, entry = await pair(hass, raw_client)
    result, phone = await _start(hass, raw_client)
    assert isinstance(phone, Phone)
    await phone.post(phone.hello())
    await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    cloud.async_delete_cloudhook.assert_awaited_once_with(hass, old_phone.webhook_id)
    assert cloud.async_get_or_create_cloudhook.await_args_list[-1].args == (
        hass,
        phone.webhook_id,
    )
    assert entry.data[CONF_CLOUDHOOK_URL] == CLOUDHOOK


async def test_a_slow_cloud_never_stops_the_entry(
    hass: HomeAssistant, raw_client: TestClient, cloud: types.ModuleType
) -> None:
    """hass_nabucasa's own timeout raises a bare TimeoutError."""
    cloud.async_get_or_create_cloudhook.side_effect = TimeoutError
    phone, entry = await pair(hass, raw_client)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert CONF_CLOUDHOOK_URL not in entry.data
    assert (await phone.post(phone.state())).ack["ok"] is True
