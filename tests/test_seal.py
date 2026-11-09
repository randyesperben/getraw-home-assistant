"""The seal (contract 3): the fixtures the phone's posters are held to."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.getraw import seal as seal_module
from custom_components.getraw.seal import (
    SEALED_TEXT_MAX,
    Direction,
    SealError,
    _seal,
    decode_key,
    encode_key,
    new_key,
    open_sealed,
    seal,
)

from .conftest import fixture_files, load

WEBHOOK_ID = "0" * 63 + "a"
# 0xFB bytes spell "-" and "_" in base64url, "+" and "/" in standard base64.
KEY_TEXT = encode_key(bytes([0xFB] * 32))


@pytest.mark.parametrize("path", fixture_files("sealed"), ids=lambda p: p.stem)
def test_fixture_opens_and_seals_again_byte_for_byte(path: Path) -> None:
    case = load(path)
    key = decode_key(case["key"])
    direction = Direction(case["direction"])
    plaintext = case["plaintext"].encode()

    assert open_sealed(key, direction, case["webhook_id"], case["sealed"]) == plaintext
    assert (
        _seal(key, direction, case["webhook_id"], plaintext, bytes.fromhex(case["nonce"]))
        == case["sealed"]
    )


@pytest.mark.parametrize(
    "path", fixture_files("sealed/invalid"), ids=lambda p: p.stem
)
def test_invalid_fixture_never_opens(path: Path) -> None:
    case = load(path)
    with pytest.raises(SealError):
        open_sealed(
            decode_key(case["key"]),
            Direction(case["direction"]),
            case["webhook_id"],
            case["sealed"],
        )


def test_every_seal_takes_a_fresh_nonce_from_the_csprng(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four sealers share one key, so a counter or a fixed nonce would let anyone forge."""
    asked: list[int] = []

    def urandom(size: int) -> bytes:
        asked.append(size)
        return bytes([len(asked)] * size)

    monkeypatch.setattr(seal_module.os, "urandom", urandom)
    key = bytes(32)
    first = seal(key, Direction.TO_PHONE, WEBHOOK_ID, b"same")
    second = seal(key, Direction.TO_PHONE, WEBHOOK_ID, b"same")

    assert asked == [12, 12]
    assert first == _seal(key, Direction.TO_PHONE, WEBHOOK_ID, b"same", bytes([1] * 12))
    assert second == _seal(key, Direction.TO_PHONE, WEBHOOK_ID, b"same", bytes([2] * 12))


@pytest.mark.parametrize("sealed", [1, None, ["A" * 40]], ids=["number", "null", "list"])
def test_only_a_string_reaches_the_cipher(sealed: object) -> None:
    """Anything else is a 400 for the webhook, never a traceback in HA's log."""
    with pytest.raises(SealError):
        open_sealed(new_key(), Direction.TO_HOME_ASSISTANT, WEBHOOK_ID, sealed)


def test_a_seal_past_the_contracts_size_never_opens() -> None:
    """A message is at most 64 KB (contract 1): a longer one is refused before the cipher."""
    key = new_key()
    sealed = seal(key, Direction.TO_HOME_ASSISTANT, WEBHOOK_ID, b"x" * 65_600)
    assert len(sealed) > SEALED_TEXT_MAX
    with pytest.raises(SealError):
        open_sealed(key, Direction.TO_HOME_ASSISTANT, WEBHOOK_ID, sealed)


@pytest.mark.parametrize("size", [16, 24])
def test_only_aes_256(size: int) -> None:
    with pytest.raises(ValueError):
        seal(bytes(size), Direction.TO_PHONE, WEBHOOK_ID, b"x")
    with pytest.raises(ValueError):
        open_sealed(bytes(size), Direction.TO_PHONE, WEBHOOK_ID, "A" * 40)


@pytest.mark.parametrize(
    "text",
    [
        KEY_TEXT + "=",  # padded
        KEY_TEXT[:-1],  # 42 characters
        KEY_TEXT[:-1] + "B",  # last character not canonical
        KEY_TEXT.replace("-", "+").replace("_", "/"),  # standard alphabet
    ],
)
def test_a_key_has_one_spelling(text: str) -> None:
    assert decode_key(KEY_TEXT) == bytes([0xFB] * 32)
    with pytest.raises(ValueError):
        decode_key(text)


def test_the_webhook_id_is_bound_as_lowercase_hex_only() -> None:
    with pytest.raises(ValueError):
        seal(new_key(), Direction.TO_PHONE, WEBHOOK_ID.upper(), b"x")
