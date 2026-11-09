"""The pairing link: Home Assistant writes the fixtures byte for byte.

The phone reads them: its tests (Dart's ``readHaLink``) read every valid
fixture to its fields and refuse every invalid one for the key it names, so
the rules live in one reader and the fixtures' README. Here Home Assistant
writes each valid link exactly, and refuses to write any URL, webhook id,
instance id or pin an invalid fixture holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from custom_components.getraw.pairing_link import build_link, certificate_pin
from custom_components.getraw.seal import decode_key

from .conftest import FIXTURES, fixture_files, load


def link_fields(link: str) -> dict[str, list[str]]:
    """Every value of a link's fragment, by key, decoded and never checked.

    The phone's reader is the one that checks; tests use this to take the
    URLs and keys back out of a link Home Assistant wrote.
    """
    values: dict[str, list[str]] = {}
    for pair in link.partition("#")[2].split("&"):
        key, _, value = pair.partition("=")
        values.setdefault(key, []).append(unquote(value))
    return values


@pytest.mark.parametrize("path", fixture_files("pairing_link"), ids=lambda p: p.stem)
def test_home_assistant_writes_each_link_the_phone_reads(path: Path) -> None:
    """``written_from``: what Home Assistant had, where the phone reads it otherwise."""
    case = load(path)
    if case.get("reader_only"):
        return  # a link Home Assistant never writes: read only
    given = case["fields"] | case.get("written_from", {})
    assert (
        build_link(
            urls=given["urls"],
            webhook_id=given["webhook_id"],
            key=decode_key(given["key"]),
            instance_id=given["instance_id"],
            name=given["name"],
            pin=given.get("pin"),
        )
        == case["link"]
    )


@pytest.mark.parametrize(
    "path",
    [
        path
        for path in fixture_files("pairing_link/invalid")
        if load(path)["field"] in ("u", "w", "i", "p")
    ],
    ids=lambda p: p.stem,
)
def test_home_assistant_never_writes_a_value_the_phone_refuses(path: Path) -> None:
    """A bad setting fails the flow, never a QR code every phone refuses.

    The other keys are no setting: ``v`` is always 1, ``k`` is written from
    32 bytes, and a name is made drawable (``name_drawn_on_one_line.json``).
    """
    case = load(path)
    values = link_fields(case["link"])
    with pytest.raises(ValueError, match=rf"^{case['field']}: "):
        build_link(
            urls=values.get("u", []),
            webhook_id=values.get("w", [""])[0],
            key=bytes(32),
            instance_id=values.get("i", [""])[0],
            name="Home",
            pin=values.get("p", [None])[0],
        )


_GOOD: dict[str, Any] = {
    "urls": ["https://192.168.1.20:8123/api/webhook/" + "a" * 64],
    "webhook_id": "a" * 64,
    "key": bytes(32),
    "instance_id": "b" * 32,
    "name": "Home",
    "pin": "FeCzi8widp0LWVVxPpt22KhyeFcrJTCIpamf9Zh-GfQ",
}


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"urls": ["http://192.168.1.20:8123/" + "a" * 500]}, "u"),
        ({"urls": ["https://homeassistant.local:8123/api/webhook/" + "a" * 64]}, "p"),
    ],
    ids=["longer_than_an_ack_carries", "pin_without_ip"],
)
def test_home_assistant_writes_only_what_an_ack_carries(
    change: dict[str, Any], field: str
) -> None:
    """A URL the ack could not repeat, a pin no URL uses: refused too."""
    build_link(**_GOOD)
    with pytest.raises(ValueError, match=rf"^{field}: "):
        build_link(**(_GOOD | change))


def test_the_name_is_trimmed_and_cut_to_64_characters() -> None:
    link = build_link(
        urls=["http://192.168.1.20:8123/api/webhook/" + "a" * 64],
        webhook_id="a" * 64,
        key=bytes(32),
        instance_id="b" * 32,
        name="  " + "ñ" * 70,
    )
    assert link_fields(link)["n"] == ["ñ" * 64]
    blank = link.rsplit("&n=", 1)[0]
    # Nothing left once the marks go: the phone refuses an empty name.
    assert build_link(
        urls=["http://192.168.1.20:8123/api/webhook/" + "a" * 64],
        webhook_id="a" * 64,
        key=bytes(32),
        instance_id="b" * 32,
        name=" \u200f\ufeff\u2028 ",
    ) == f"{blank}&n=Home%20Assistant"


def test_each_certificate_pins_its_leaf() -> None:
    folder = FIXTURES / "pairing_link" / "pins"
    pins = load(folder / "pins.json")["pins"]
    # Every certificate has its pin and every pin its certificate, read from the
    # folder itself rather than from a list copied here.
    certificates = {path.name for path in folder.glob("*.pem")}
    assert certificates, f"no certificates in {folder}"
    assert set(pins) == certificates
    for name, pin in pins.items():
        assert certificate_pin((folder / name).read_bytes()) == pin, name
