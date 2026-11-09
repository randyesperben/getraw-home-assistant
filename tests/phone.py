"""A phone for the tests: it seals posts as RAW's poster does and opens the acks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
from typing import Any
from urllib.parse import urlsplit

from aiohttp.test_utils import TestClient

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.getraw.const import DOMAIN
from custom_components.getraw.seal import Direction, decode_key, open_sealed, seal

from .conftest import FIXTURES, load
from .test_pairing_link import read_link

ACCOUNT = "fdd0f8e35730fdd04cd0dd0505bbc0b02036a2e6b4e346ecaf6b3db49e2b241d"
OTHER_ACCOUNT = "0" * 64


def instant(moment: datetime) -> str:
    """A contract instant: UTC, three-digit milliseconds."""
    return moment.astimezone(dt_util.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{moment.microsecond // 1000:03d}Z"
    )


@dataclass
class Answer:
    status: int
    ack: dict[str, Any] | None


@dataclass
class Phone:
    """One phone holding one pairing: the link's webhook id, path and keys."""

    client: TestClient
    webhook_id: str
    path: str
    key: str
    account_ref: str = ACCOUNT
    revision: int = 1_762_700_000_000
    keys_seen: list[str] = field(default_factory=list)
    # How far the phone's clock runs behind Home Assistant's.
    behind: timedelta = timedelta(0)

    @classmethod
    def from_link(cls, client: TestClient, link: str, **kwargs: Any) -> Phone:
        fields = read_link(link)
        path = urlsplit(fields["urls"][0]).path
        return cls(client, fields["webhook_id"], path, fields["key"], **kwargs)

    def body(self, kind: str, **extra: Any) -> dict[str, Any]:
        """An envelope as the poster stamps it, with the next live revision."""
        self.revision += 1
        return {
            "schema_version": 1,
            "kind": kind,
            "account_ref": self.account_ref,
            "revision": self.revision,
            "phone_now": instant(dt_util.utcnow() - self.behind),
            **extra,
        }

    def hello(self, **extra: Any) -> dict[str, Any]:
        return self.body(
            "hello",
            display_name="Randy",
            app={"version": "1.4.0", "build": 140},
            platform="ios",
            writes=[1],
            **extra,
        )

    def state(self, phase: str = "idle", **extra: Any) -> dict[str, Any]:
        return self.body("state", live={"phase": phase}, categories=[], **extra)

    def event(self, *events: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return self.body(
            "event", events=list(events), live={"phase": "active"}, categories=[], **extra
        )

    async def post(
        self, body: dict[str, Any] | bytes, *, key: str | None = None
    ) -> Answer:
        """Seal ``body`` (or post raw bytes) and open the ack under the same key."""
        used = key or self.key
        if isinstance(body, bytes):
            payload = body
        else:
            sealed = seal(
                decode_key(used),
                Direction.TO_HOME_ASSISTANT,
                self.webhook_id,
                json.dumps(body).encode(),
            )
            payload = json.dumps({"sealed": sealed}).encode()
        response = await self.client.post(self.path, data=payload)
        text = await response.read()
        if not text:
            return Answer(response.status, None)
        opened = open_sealed(
            decode_key(used), Direction.TO_PHONE, self.webhook_id, json.loads(text)["sealed"]
        )
        ack = json.loads(opened)
        if (offered := ack.get("key")) is not None:
            self.keys_seen.append(offered)
        return Answer(response.status, ack)


def event_of(type_: str, occurred: datetime, started: int = 1_762_700_000_000) -> dict[str, Any]:
    """One event with contract 1's id: started_at ms, type, occurred_at ms."""
    occurred_ms = round(occurred.timestamp() * 1000)
    return {
        "event_id": f"{started}:{type_}:{occurred_ms}",
        "type": type_,
        "occurred_at": instant(occurred),
        "data": {},
    }


def ago(seconds: float) -> datetime:
    return dt_util.utcnow() - timedelta(seconds=seconds)


def runtime_of(hass: HomeAssistant):  # noqa: ANN201
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return entry.runtime_data


ALL_CATEGORIES = ["titles", "effort", "body_weight", "measurements", "saved_volume", "notes"]
STARTED = datetime(2026, 11, 9, 9, 0, tzinfo=dt_util.UTC)
EXERCISE = "0199fd1c-8a40-7b2e-9c41-5d2a7e1f0e01"
SET = "0199fd1c-8a40-7b2e-9c41-5d2a7e1f0c13"


def workout(**changes: Any) -> dict[str, Any]:
    """A live workout shaped as contract 1's ``ha_state/active`` fixture, started at STARTED."""
    last = dt_util.utcnow()
    live: dict[str, Any] = {
        "phase": "active",
        "started_at": instant(STARTED),
        "last_activity_at": instant(last),
        "abandoned_at": instant(last + timedelta(hours=3)),
        "completed_sets": 2,
        "total_sets": 9,
        "volume_kg": 1280,
        "exercise": {
            "id": EXERCISE,
            "name": "Bench Press",
            "index": 1,
            "total": 3,
            "logging_type": "weight_reps",
        },
        "set": {
            "id": SET,
            "index": 3,
            "total": 3,
            "set_type": "normal",
            "target": {"weight_kg": 80, "reps": 8, "reps_max": 10},
        },
        "text": {
            "exercise": "Bench Press, 1 of 3",
            "set": "Set 3 of 3",
            "target": "80 kg × 8–10",
            "previous": "Last time: 77.5 kg × 8",
            "next": "Next: Overhead Press, 47.5 kg × 8",
        },
        "health": {
            "titles": {"title": "Push A"},
            "effort": {"target_rpe": 8, "previous_rpe": 7.5},
        },
        "weight_unit": "kg",
        "distance_unit": "km",
        "locale": "en",
    }
    live.update(changes)
    return live


def resting(seconds: float, **changes: Any) -> dict[str, Any]:
    """The workout resting since now, its rest ending ``seconds`` from now."""
    now = dt_util.utcnow()
    return workout(
        phase="resting",
        rest_started_at=instant(now),
        rest_ends_at=instant(now + timedelta(seconds=seconds)),
        **changes,
    )


def summary_fixture(name: str) -> dict[str, Any]:
    """A summary fixture of contracts/home_assistant, its envelope left for the phone to stamp."""
    body = load(FIXTURES / "ha_summary" / f"{name}.json")
    for key in ("schema_version", "kind", "account_ref", "revision", "phone_now"):
        body.pop(key)
    return body


def entity_state(hass: HomeAssistant, platform: str, key: str):  # noqa: ANN201
    """The state of RAW's entity ``key``, found by its unique id."""
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{ACCOUNT}_{key}")
    assert entity_id is not None, f"no {platform} {key}"
    return hass.states.get(entity_id)
