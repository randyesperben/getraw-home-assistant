"""What the phone posts and what Home Assistant answers.

The reader is tolerant, as the TV's is: an unknown key or event type is ignored
and only an unknown ``schema_version`` is refused. The phone's writers are held
strict by the messages' JSON schemas (``*.schema.json``); this module checks only
what Home Assistant must trust before it acts on a message.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
from typing import Any, Final

from aiohttp import web

SCHEMA_VERSION: Final = 1
KINDS: Final = ("hello", "state", "event", "patch", "summary", "bye")
# The schema versions this release reads, per kind (the ack's `reads`).
READS: Final[Mapping[str, tuple[int, ...]]] = {kind: (1,) for kind in KINDS}
# Contract 1's two revision sequences: every kind that moves the live state
# shares one, the summary has its own.
SEQUENCE: Final[Mapping[str, str]] = {
    "hello": "live",
    "state": "live",
    "event": "live",
    "patch": "live",
    "bye": "live",
    "summary": "summary",
}
BYE_REASONS: Final = ("disconnected", "signed_out", "account_deleted", "paused")
MAX_EVENTS: Final = 20
# The ack echoes a revision as a JSON number every reader holds exactly
# (ha_ack.schema.json); a larger one would freeze its sequence until a re-pair.
REVISION_MAX: Final = 2**53 - 1

# A message is at most 64 000 UTF-8 bytes before the seal; sealed
# and in base64 that is about 85 400 bytes. Anything longer is not RAW's.
MAX_BODY_BYTES: Final = 96 * 1024

_ACCOUNT_REF = re.compile(r"[0-9a-f]{64}")
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


class Refused(Exception):
    """A message that opened but is answered ``ok: false`` with this reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Message:
    """An opened message whose envelope can be answered."""

    kind: str
    revision: int
    body: dict[str, Any]

    @property
    def sequence(self) -> str:
        return SEQUENCE[self.kind]


@dataclass(frozen=True, slots=True)
class Event:
    """One event of an ``event`` or ``patch`` message."""

    event_id: str
    type: str
    occurred_at: datetime
    raw: dict[str, Any]


async def read_body(request: web.Request) -> bytes | None:
    """The request's body, or None past ``MAX_BODY_BYTES``, read without trusting its length."""
    chunks: list[bytes] = []
    size = 0
    while chunk := await request.content.read(64 * 1024):
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def sealed_value(body: bytes) -> str | None:
    """The ``sealed`` string of ``{"sealed": "…"}``, or None for any other body."""
    try:
        decoded = json.loads(body)
    except ValueError:
        return None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("sealed"), str):
        return None
    return decoded["sealed"]


def parse_instant(value: Any) -> datetime | None:
    """A contract instant (UTC, three-digit milliseconds), or None."""
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_message(plaintext: bytes) -> Message | None:
    """The message's kind, revision and body, or None when it cannot be answered.

    A plaintext that is not an object with a known ``kind`` and a whole
    ``revision`` up to ``REVISION_MAX`` gets no ack: the phone keys every ack on
    those two.
    """
    try:
        body = json.loads(plaintext)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    kind, revision = body.get("kind"), body.get("revision")
    if kind not in KINDS or not _is_int(revision) or not 0 <= revision <= REVISION_MAX:
        return None
    return Message(kind=kind, revision=revision, body=body)


def check_envelope(message: Message, account_ref: str | None) -> datetime:
    """The message's ``phone_now``, or ``Refused`` with the reason to answer.

    ``account_ref`` None accepts any account (the pairing hook's first hello).
    """
    body = message.body
    version = body.get("schema_version")
    if not _is_int(version):
        raise Refused("invalid")
    if version not in READS[message.kind]:
        raise Refused("unsupported_version")
    ref = body.get("account_ref")
    if not isinstance(ref, str) or not _ACCOUNT_REF.fullmatch(ref):
        raise Refused("invalid")
    if account_ref is not None and ref != account_ref:
        raise Refused("wrong_account")
    phone_now = parse_instant(body.get("phone_now"))
    if phone_now is None:
        raise Refused("invalid")
    _check_body(message)
    return phone_now


def _check_body(message: Message) -> None:
    """What each kind needs before Home Assistant acts on it."""
    body = message.body
    match message.kind:
        case "hello":
            name = body.get("display_name")
            if name is not None and not isinstance(name, str):
                raise Refused("invalid")
        case "state" | "event" | "patch":
            if not isinstance(body.get("live"), dict):
                raise Refused("invalid")
            if message.kind != "state":
                events_of(message)
        case "summary":
            pass
        case "bye":
            if body.get("reason") not in BYE_REASONS:
                raise Refused("invalid")


def events_of(message: Message) -> list[Event]:
    """The events of an ``event`` or ``patch`` message, in the order they happened."""
    raw = message.body.get("events", [])
    if not isinstance(raw, list) or len(raw) > MAX_EVENTS:
        raise Refused("invalid")
    if message.kind == "event" and not raw:
        raise Refused("invalid")
    events: list[Event] = []
    for item in raw:
        if not isinstance(item, dict):
            raise Refused("invalid")
        event_id, type_ = item.get("event_id"), item.get("type")
        occurred_at = parse_instant(item.get("occurred_at"))
        if (
            not isinstance(event_id, str)
            or not 0 < len(event_id) <= 128
            or not isinstance(type_, str)
            or occurred_at is None
        ):
            raise Refused("invalid")
        events.append(Event(event_id, type_, occurred_at, item))
    return events


def ack(
    message: Message,
    *,
    urls: list[dict[str, str]] | None = None,
    key: str | None = None,
    pending: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """The ack to ``message`` (``ha_ack.schema.json``): ``ok`` exactly when there is no reason."""
    answer: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": reason is None,
        "kind": message.kind,
        "revision": message.revision,
        "reads": {kind: list(versions) for kind, versions in READS.items()},
    }
    if reason is not None:
        answer["reason"] = reason
        return answer
    answer["urls"] = urls or []
    if key is not None:
        answer["key"] = key
    if pending:
        answer["pending"] = True
    return answer
