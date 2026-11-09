"""The pairing link Home Assistant shows as a QR code (contract 4, ADR 0173).

``https://getraw.fit/ha#v=1&u=<url>&u=<url>&w=<webhook id>&k=<key>&i=<instance id>&p=<pin>&n=<name>``

Everything travels in the fragment, which browsers never send to getraw.fit.
``u`` is a URL the phone posts to, in the order it tries them; ``w`` the
webhook id the seal binds; ``k`` the single-use pairing key; ``i`` Home
Assistant's instance id, which keys the phone's ``account_ref``; ``p`` the pin
of Home Assistant's own TLS certificate, checked instead of the system's trust
on every ``https`` ``u`` whose host is an IP address; ``n`` the name RAW shows.
The rules a reader applies: ``contracts/home_assistant/fixtures/pairing_link/README.md``.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
import hashlib
from ipaddress import ip_address
import re
from urllib.parse import quote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from .seal import encode_key

LINK_BASE = "https://getraw.fit/ha"
LINK_VERSION = 1
NAME_MAX = 64
URLS_MAX = 4
# The ack carries each URL again, at most this long (ha_ack.schema.json).
URL_MAX = 512
FALLBACK_NAME = "Home Assistant"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
# SHA-256 in base64url without padding: its last character holds 4 bits.
_PIN = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")


def build_link(
    *,
    urls: Sequence[str],
    webhook_id: str,
    key: bytes,
    instance_id: str,
    name: str,
    pin: str | None = None,
) -> str:
    """The link, its keys always in this order and every value percent-encoded.

    ValueError for anything the README's reader refuses, so Home Assistant
    never shows a code no phone can read (AUDIT-L2-1 M7).
    """
    _check(urls, webhook_id, instance_id, pin)
    shown = name.strip()[:NAME_MAX].strip() or FALLBACK_NAME
    pairs: list[tuple[str, str]] = [("v", str(LINK_VERSION))]
    pairs += [("u", url) for url in urls]
    pairs += [("w", webhook_id), ("k", encode_key(key)), ("i", instance_id)]
    if pin is not None:
        pairs.append(("p", pin))
    pairs.append(("n", shown))
    fragment = "&".join(f"{k}={quote(v, safe='')}" for k, v in pairs)
    return f"{LINK_BASE}#{fragment}"


def _check(
    urls: Sequence[str], webhook_id: str, instance_id: str, pin: str | None
) -> None:
    if not 1 <= len(urls) <= URLS_MAX:
        raise ValueError(f"u: a pairing link carries 1 to {URLS_MAX} URLs")
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("u: an http or https URL")
        if len(url) > URL_MAX:
            raise ValueError(f"u: at most {URL_MAX} characters")
    if not _HEX64.fullmatch(webhook_id):
        raise ValueError("w: 64 lowercase hex characters")
    if not _HEX32.fullmatch(instance_id):
        raise ValueError("i: 32 lowercase hex characters")
    if pin is None:
        return
    if not _PIN.fullmatch(pin):
        raise ValueError("p: a SHA-256 in base64url without padding")
    if not any(_tls_to_ip(url) for url in urls):
        raise ValueError("p: only with an https URL whose host is an IP address")


def _tls_to_ip(url: str) -> bool:
    parts = urlsplit(url)
    try:
        ip_address(parts.hostname or "")
    except ValueError:
        return False
    return parts.scheme == "https"


def certificate_pin(pem: bytes) -> str:
    """SHA-256 of the leaf certificate's DER, base64url without padding.

    The leaf is the first certificate of the file Home Assistant serves
    (``http: ssl_certificate``, often a full chain). The whole certificate is
    hashed, not its public key: iOS hands out a key without its SubjectPublicKeyInfo
    header, and every ack carries the pin again, so a renewal costs nothing
    while another route still answers.
    """
    leaf = x509.load_pem_x509_certificates(pem)[0]
    digest = hashlib.sha256(leaf.public_bytes(Encoding.DER)).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
