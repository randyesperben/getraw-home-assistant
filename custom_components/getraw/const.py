"""Names that are forever once released (``entities.md``).

The domain, every event type and every entity key become parts of users'
automations, entity ids and statistics. A name here is added, never renamed or
removed; the phone's ``haEventsOf`` writes only these event types.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "getraw"

# What the phone writes (an event's `type`), then what Home Assistant fires
# itself: `rest_ended` at the rest's end, `workout_abandoned` at `abandoned_at`.
PHONE_EVENT_TYPES: Final = (
    "workout_started",
    "set_completed",
    "exercise_changed",
    "rest_started",
    "rest_adjusted",
    "rest_skipped",
    "record",
    "workout_finishing",
    "workout_discarded",
    "workout_finished",
    "test",
)
HOME_ASSISTANT_EVENT_TYPES: Final = ("rest_ended", "workout_abandoned")
EVENT_TYPES: Final = PHONE_EVENT_TYPES + HOME_ASSISTANT_EVENT_TYPES

# The config entry's data. The keys are base64url text (`seal.encode_key`): `key` is
# the one the phone last confirmed, `next_key` one offered in acks until a post
# sealed under it arrives.
CONF_KEY: Final = "key"
CONF_NEXT_KEY: Final = "next_key"
CONF_CLOUDHOOK_URL: Final = "cloudhook_url"
CONF_DISPLAY_NAME: Final = "display_name"
CONF_APP_VERSION: Final = "app_version"
CONF_PLATFORM: Final = "platform"

# The health categories (the keys of a message's `health`), each a switch on the phone.
HEALTH_CATEGORIES: Final = (
    "titles",
    "effort",
    "body_weight",
    "measurements",
    "saved_volume",
    "notes",
)
