"""The URLs the phone posts to, in the order to try them (an ack's ``urls``, a link's ``u``).

Built from Home Assistant's own network settings every time, so an ack always
carries today's: the internal URL; the instance's own IP address, which iOS
reaches over plain HTTP where a qualified local name (``ha.fritz.box``) is
blocked by App Transport Security, and which is the only local route when
Home Assistant serves TLS itself, pinned to its certificate; the external
URL; the Home Assistant Cloud webhook.

The own IP address goes beside an internal URL that is a name only when that
name resolves to it. Inside a container without host networking, Home
Assistant's own address is the container's, which no phone reaches (OrbStack's
bridge in RAW's device pass; a NAS's Docker network alike), while the name the
person set reaches the host. With no internal URL set, the local route is the
address Home Assistant picks for itself, as its own Network settings show it.

Plain ``http://`` goes only where a post stays on the person's own network, the
same hosts the phone allows a custom webhook over plain HTTP (the contracts'
README): an RFC 1918, link-local or 100.64.0.0/10 (Tailscale) IPv4 address, a
unique-local or link-local IPv6 one, a ``.local`` name or a name without a dot
other than ``localhost``, all digits or ``0x…`` (a resolver may read
``134744072`` or ``0x08080808`` as a public address); the table both read is the
contracts' ``fixtures/webhook_url/rule.json``. The bodies are sealed, but a public
name or address would carry the webhook id and every post's timing across the
internet. No route is ever the loopback address, which Home Assistant falls back
to with no network.

Every URL is in the form a URL parser writes back (no default port), the only
form the phone takes in a pairing link.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
import logging
import re
import socket
import ssl
from typing import Final
from urllib.parse import urlsplit

from homeassistant.components.webhook import async_generate_path
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util
from homeassistant.util.network import normalize_url

from .pairing_link import URLS_MAX, certificate_pin

LOCAL: Final = "local"
REMOTE: Final = "remote"
CLOUD: Final = "cloud"
_PIN: Final = "getraw_certificate_pin"
_RESOLVED: Final = "getraw_resolved"
# An ack waits for a name's lookup at most this long, at most once in this while.
_RESOLVE_SECONDS: Final = 2
_RESOLVED_FOR: Final = timedelta(minutes=10)
# A server bound to every address answers on the loopback one.
_LOOPBACK: Final = {"0.0.0.0": "127.0.0.1", "::": "::1"}
# The person's own network, as the phone reads it for a webhook over plain HTTP.
_OWN_NETWORKS: Final = tuple(
    ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
        "fe80::/10",
    )
)
_HANDSHAKE_SECONDS: Final = 10
_DIGITS: Final = re.compile(r"[0-9]+")

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Route:
    url: str
    route: str
    pin: str | None = None

    def as_ack(self) -> dict[str, str]:
        item = {"url": self.url, "route": self.route}
        if self.pin is not None:
            item["pin"] = self.pin
        return item


async def async_certificate_pin(hass: HomeAssistant) -> str | None:
    """The pin of the certificate Home Assistant serves, or None without TLS.

    Hashed from what the server hands out, never read from the file: Home
    Assistant loads its certificate once at start, so a file renewed since then
    is not served until the next restart. The
    server listens before any config entry is set up (the frontend, which
    starts it, is set up in bootstrap's stage 0), and the pin is kept for the
    run once read.
    """
    if hass.config.api is None or not hass.config.api.use_ssl:
        return None
    if (pin := hass.data.get(_PIN)) is not None:
        return pin
    host = (hass.http.server_host or ["0.0.0.0"])[0]
    address = (_LOOPBACK.get(host, host), hass.config.api.port)
    try:
        pem = await hass.async_add_executor_job(_served_certificate, address)
    except OSError as err:
        # ssl.SSLError and timeouts included. The IP route goes without a pin
        # and the next ack tries again; only the type is logged.
        _LOGGER.warning("Home Assistant's certificate not read: %s", type(err).__name__)
        return None
    hass.data[_PIN] = pin = certificate_pin(pem.encode("ascii"))
    return pin


def _served_certificate(address: tuple[str, int]) -> str:
    """The leaf certificate a TLS server at ``address`` hands out, as PEM."""
    return ssl.get_server_certificate(address, timeout=_HANDSHAKE_SECONDS)


def _ip(host: str | None) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(host or "")
    except ValueError:
        return None


async def _own_address(hass: HomeAssistant, internal: str) -> str | None:
    """Home Assistant's own IP route, when the internal URL is a name that resolves to it."""
    api = hass.config.api
    host = urlsplit(internal).hostname
    if api is None or not api.local_ip or host is None or _ip(host) is not None:
        return None
    if api.local_ip not in await _resolved(hass, host):
        return None
    address = f"[{api.local_ip}]" if ":" in api.local_ip else api.local_ip
    # Without a default port, as get_url writes the internal URL beside it.
    return normalize_url(f"{'https' if api.use_ssl else 'http'}://{address}:{api.port}")


async def _resolved(hass: HomeAssistant, host: str) -> set[str]:
    """The addresses ``host`` resolves to here, looked up at most once every ten minutes."""
    cache: dict[str, tuple[datetime, set[str]]] = hass.data.setdefault(_RESOLVED, {})
    now = dt_util.utcnow()
    if (kept := cache.get(host)) is not None and kept[0] > now:
        return kept[1]
    try:
        async with asyncio.timeout(_RESOLVE_SECONDS):
            found = await _addresses_of(host)
    except TimeoutError:
        found = set()
    cache[host] = (now + _RESOLVED_FOR, found)
    return found


async def _addresses_of(host: str) -> set[str]:
    """Every address ``host`` resolves to on this machine; none when it does not."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except OSError:
        return set()
    return {str(info[4][0]) for info in infos}


def _offered(scheme: str, host: str | None) -> bool:
    """Whether a phone may post to ``host``: see the module's rules for plain HTTP."""
    address = _ip(host)
    if address is not None and (address.is_loopback or address.is_unspecified):
        return False
    if scheme != "http":
        return True
    if address is not None:
        return any(address in network for network in _OWN_NETWORKS)
    name = (host or "").rstrip(".").lower()
    if "." in name:
        return name.endswith(".local")
    return (
        bool(name)
        and name != "localhost"
        and not name.startswith("0x")
        and not _DIGITS.fullmatch(name)
    )


async def routes(
    hass: HomeAssistant,
    webhook_id: str,
    *,
    pin: str | None,
    cloudhook_url: str | None,
) -> list[Route]:
    """Every route to ``webhook_id`` now, local first, at most four."""
    path = async_generate_path(webhook_id)
    found: list[Route] = []

    def add(base: str, route: str) -> None:
        url = base.rstrip("/") + path
        if any(known.url == url for known in found):
            return
        parts = urlsplit(url)
        if not _offered(parts.scheme, parts.hostname):
            return
        tls_ip = parts.scheme == "https" and _ip(parts.hostname) is not None
        found.append(Route(url, route, pin if tls_ip else None))

    try:
        internal = get_url(hass, allow_external=False, allow_cloud=False, allow_ip=True)
    except NoURLAvailableError:
        internal = None
    if internal is not None:
        add(internal, LOCAL)
        if (own := await _own_address(hass, internal)) is not None:
            add(own, LOCAL)
    try:
        add(get_url(hass, allow_internal=False, allow_cloud=False, allow_ip=True), REMOTE)
    except NoURLAvailableError:
        pass
    if cloudhook_url:
        found.append(Route(cloudhook_url, CLOUD))
    return found[:URLS_MAX]
