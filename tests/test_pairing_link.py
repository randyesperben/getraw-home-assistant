"""The pairing link: Home Assistant writes the fixtures byte for byte.

The phone reads them. The reader below is the phone's reading rules in code,
so every invalid fixture is proven invalid for the rule it names, and every
valid one says what its fields claim.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote

import pytest

from custom_components.getraw.pairing_link import (
    NAME_MAX,
    build_link,
    certificate_pin,
)
from custom_components.getraw.seal import decode_key

from .conftest import FIXTURES, fixture_files, load

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_B64URL32 = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
_URL = re.compile(r"https?://[^\s/?#]+\S*")


def read_link(link: str) -> dict[str, Any]:
    """The fields of a link, or ValueError, by the phone's reading rules.

    Each error starts with the key it is about (``link`` for the link's own
    form), as each invalid fixture's ``field`` names it.
    """
    head, hash_sign, fragment = link.partition("#")
    if not hash_sign:
        raise ValueError("link: no fragment")
    if head != "https://getraw.fit/ha":
        raise ValueError("link: not https://getraw.fit/ha")
    values: dict[str, list[str]] = {}
    for pair in fragment.split("&"):
        key, _, value = pair.partition("=")
        values.setdefault(unquote(key), []).append(
            unquote(value, errors="strict")
        )
    counts = {"v": (1, 1), "u": (1, 4), "w": (1, 1), "k": (1, 1), "i": (1, 1)}
    counts |= {"p": (0, 1), "n": (1, 1)}
    for key, (low, high) in counts.items():
        if not low <= len(values.get(key, [])) <= high:
            raise ValueError(f"{key}: outside its count")
    if values["v"] != ["1"]:
        raise ValueError("v: a link version this app does not read")
    if not all(_URL.fullmatch(url) for url in values["u"]):
        raise ValueError("u: not an http or https URL")
    if not _HEX64.fullmatch(values["w"][0]):
        raise ValueError("w: out of form")
    if not _HEX32.fullmatch(values["i"][0]):
        raise ValueError("i: out of form")
    try:
        decode_key(values["k"][0])
    except ValueError as err:
        raise ValueError(f"k: {err}") from err
    if "p" in values and not _B64URL32.fullmatch(values["p"][0]):
        raise ValueError("p: out of form")
    if not 1 <= len(values["n"][0]) <= NAME_MAX:
        raise ValueError("n: out of form")
    fields: dict[str, Any] = {
        "version": 1,
        "urls": values["u"],
        "webhook_id": values["w"][0],
        "key": values["k"][0],
        "instance_id": values["i"][0],
    }
    if "p" in values:
        fields["pin"] = values["p"][0]
    fields["name"] = values["n"][0]
    return fields


@pytest.mark.parametrize("path", fixture_files("pairing_link"), ids=lambda p: p.stem)
def test_home_assistant_writes_the_link_and_a_reader_gets_its_fields(
    path: Path,
) -> None:
    case = load(path)
    fields = case["fields"]
    assert read_link(case["link"]) == fields
    if path.stem == "unknown_key":
        return  # a later release's key: this release never writes it
    assert (
        build_link(
            urls=fields["urls"],
            webhook_id=fields["webhook_id"],
            key=decode_key(fields["key"]),
            instance_id=fields["instance_id"],
            name=fields["name"],
            pin=fields.get("pin"),
        )
        == case["link"]
    )


@pytest.mark.parametrize(
    "path", fixture_files("pairing_link/invalid"), ids=lambda p: p.stem
)
def test_every_invalid_link_is_refused_for_its_reason(path: Path) -> None:
    """Refused for the key its fixture names, never for another."""
    case = load(path)
    with pytest.raises(ValueError, match=rf"^{case['field']}: "):
        read_link(case["link"])


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
        ({"urls": ["raw-live://open"]}, "u"),
        ({"urls": ["http://192.168.1.20:8123/" + "a" * 500]}, "u"),
        ({"webhook_id": "A" * 64}, "w"),
        ({"instance_id": "b" * 31}, "i"),
        ({"pin": _GOOD["pin"] + "="}, "p"),
        ({"urls": ["https://homeassistant.local:8123/api/webhook/" + "a" * 64]}, "p"),
    ],
    ids=["scheme", "too_long", "webhook_id", "instance_id", "pin_form", "pin_without_ip"],
)
def test_home_assistant_never_writes_a_link_no_phone_reads(
    change: dict[str, Any], field: str
) -> None:
    """A bad setting fails the flow, never a QR code every phone refuses."""
    read_link(build_link(**_GOOD))
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
    assert read_link(link)["name"] == "ñ" * 64
    blank = link.rsplit("&n=", 1)[0]
    assert build_link(
        urls=["http://192.168.1.20:8123/api/webhook/" + "a" * 64],
        webhook_id="a" * 64,
        key=bytes(32),
        instance_id="b" * 32,
        name="   ",
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
