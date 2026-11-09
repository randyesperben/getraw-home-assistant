"""The URLs the phone posts to, in the order to try them (contracts 2 and 4).

Built from Home Assistant's own network settings every time, so an ack always
carries today's: the internal URL; the instance's own IP address, which iOS
reaches over plain HTTP where a qualified local name (``ha.fritz.box``) is
blocked by App Transport Security (the plan audit's F-I4), and which is the
only local route when Home Assistant serves TLS itself, pinned to its
certificate (F-I3); the external URL; the Home Assistant Cloud webhook.

Plain ``http://`` goes only where a post stays on the local network: a
private or link-local IP address, a ``.local`` name or a name without a dot. A
public name or address would carry the webhook id and every post's timing
across the internet (AUDIT-L2-1 M9). No route is ever the loopback address,
which Home Assistant falls back to with no network (AUDIT-L2-2 M8).
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
import logging
import ssl
from typing import Final
from urllib.parse import urlsplit

from homeassistant.components.webhook import async_generate_path
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .pairing_link import URLS_MAX, certificate_pin

LOCAL: Final = "local"
REMOTE: Final = "remote"
CLOUD: Final = "cloud"
_PIN: Final = "getraw_certificate_pin"
# A server bound to every address answers on the loopback one.
_LOOPBACK: Final = {"0.0.0.0": "127.0.0.1", "::": "::1"}
_HANDSHAKE_SECONDS: Final = 10

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
    is not served until the next restart (AUDIT-L2-1 M8, AUDIT-L2-2 M4). The
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


def _offered(scheme: str, host: str | None) -> bool:
    """Whether a phone may post to ``host``: see the module's rules for plain HTTP."""
    address = _ip(host)
    if address is not None and (address.is_loopback or address.is_unspecified):
        return False
    if scheme != "http":
        return True
    if address is not None:
        return address.is_private or address.is_link_local
    name = (host or "").rstrip(".").lower()
    return bool(name) and ("." not in name or name.endswith(".local"))


def routes(
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
        add(get_url(hass, allow_external=False, allow_cloud=False, allow_ip=True), LOCAL)
    except NoURLAvailableError:
        pass
    if (api := hass.config.api) is not None and api.local_ip:
        host = f"[{api.local_ip}]" if ":" in api.local_ip else api.local_ip
        scheme = "https" if api.use_ssl else "http"
        add(f"{scheme}://{host}:{api.port}", LOCAL)
    try:
        add(get_url(hass, allow_internal=False, allow_cloud=False, allow_ip=True), REMOTE)
    except NoURLAvailableError:
        pass
    if cloudhook_url:
        found.append(Route(cloudhook_url, CLOUD))
    return found[:URLS_MAX]
