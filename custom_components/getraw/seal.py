"""The seal every RAW message and every ack travels under.

A sealed body is ``{"sealed": "<base64>"}``: standard base64 with padding of
nonce (12 bytes) ‖ ciphertext ‖ tag (16 bytes), AES-256-GCM under the pairing
key. The associated data is the direction (``p2h:`` from the phone, ``h2p:``
from Home Assistant) followed by the 64-character hex webhook id as UTF-8, so a
captured ack can never be replayed as a message, nor a message as an ack.

The nonce is 12 bytes from the operating system's CSPRNG for every seal: four
sealers share one key (Home Assistant, the phone's app, Android's ``:session``,
iOS's lock-screen intent), and a repeated GCM nonce would leak the plaintexts'
XOR and let anyone forge. Only the fixtures' test calls ``_seal`` with a nonce.
"""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
# base64 of the largest sealed message (the phone caps a message at 64 KB; here
# 64 KiB) with its nonce and tag: 4 * ceil((65 536 + 28) / 3).
SEALED_TEXT_MAX = 87_420

# A key as the pairing link and the ack carry it: base64url, no padding.
_KEY_TEXT = re.compile(r"[A-Za-z0-9_-]{43}")
_WEBHOOK_ID = re.compile(r"[0-9a-f]{64}")


class Direction(StrEnum):
    """Who sealed it, bound into the associated data."""

    TO_HOME_ASSISTANT = "p2h"
    TO_PHONE = "h2p"


class SealError(Exception):
    """A body that does not open. The sender is never told why."""


def associated_data(direction: Direction, webhook_id: str) -> bytes:
    """``p2h:`` or ``h2p:`` and the webhook id, as UTF-8 bytes."""
    if not _WEBHOOK_ID.fullmatch(webhook_id):
        raise ValueError("a webhook id is 64 lowercase hex characters")
    return f"{direction}:{webhook_id}".encode()


def new_key() -> bytes:
    """A fresh 32-byte key from the CSPRNG."""
    return os.urandom(KEY_BYTES)


def seal(key: bytes, direction: Direction, webhook_id: str, plaintext: bytes) -> str:
    """Seal ``plaintext`` under a fresh nonce: the value of the body's ``sealed`` key."""
    return _seal(key, direction, webhook_id, plaintext, os.urandom(NONCE_BYTES))


def _seal(
    key: bytes, direction: Direction, webhook_id: str, plaintext: bytes, nonce: bytes
) -> str:
    _check_key(key)
    if len(nonce) != NONCE_BYTES:
        raise ValueError("a nonce is 12 bytes")
    sealed = AESGCM(key).encrypt(
        nonce, plaintext, associated_data(direction, webhook_id)
    )
    return base64.b64encode(nonce + sealed).decode("ascii")


def _check_key(key: bytes) -> None:
    """AES-256 only: AESGCM would also take a 16- or 24-byte key."""
    if len(key) != KEY_BYTES:
        raise ValueError("a key is 32 bytes")


def open_sealed(
    key: bytes, direction: Direction, webhook_id: str, sealed: object
) -> bytes:
    """The plaintext under ``sealed``, or ``SealError``: the one gate for what arrives."""
    _check_key(key)
    if not isinstance(sealed, str) or len(sealed) > SEALED_TEXT_MAX:
        raise SealError
    try:
        raw = base64.b64decode(sealed, validate=True)
    except (binascii.Error, ValueError) as err:
        raise SealError from err
    # Only the text an encoder writes: the platforms' decoders disagree on
    # missing padding and stray bits, and encoding again is the one rule
    # all three can apply.
    if base64.b64encode(raw).decode("ascii") != sealed:
        raise SealError
    if len(raw) < NONCE_BYTES + TAG_BYTES:
        raise SealError
    try:
        return AESGCM(key).decrypt(
            raw[:NONCE_BYTES],
            raw[NONCE_BYTES:],
            associated_data(direction, webhook_id),
        )
    except InvalidTag as err:
        raise SealError from err


def encode_key(key: bytes) -> str:
    """A key as text: base64url without padding, 43 characters."""
    _check_key(key)
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


def decode_key(text: str) -> bytes:
    """The 32 bytes behind ``encode_key``'s text; refuses any other spelling."""
    if not _KEY_TEXT.fullmatch(text):
        raise ValueError("a key is 43 base64url characters without padding")
    key = base64.urlsafe_b64decode(text + "=")
    if encode_key(key) != text:
        raise ValueError("a key's last character must be canonical")
    return key
