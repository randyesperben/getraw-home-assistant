"""Shared paths for the RAW integration's tests."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
import json
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

_HERE = Path(__file__).parent
# The public repository carries a copy of contracts/home_assistant in tests/contracts,
# made by RAW's publish script; in RAW's repository it is read where it lives.
CONTRACTS = (
    _HERE / "contracts"
    if (_HERE / "contracts").is_dir()
    else _HERE.parent.parent / "contracts" / "home_assistant"
)
FIXTURES = CONTRACTS / "fixtures"


def fixture_files(folder: str) -> list[Path]:
    """The fixtures of one folder, refusing an empty one: a test over nothing passes."""
    files = sorted((FIXTURES / folder).glob("*.json"))
    if not files:
        raise AssertionError(f"no fixtures in {FIXTURES / folder}")
    return files


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/getraw in every test."""


@pytest.fixture
async def raw_client(
    hass: HomeAssistant,
    hass_client_no_auth: Callable[[], Coroutine[Any, Any, TestClient]],
) -> TestClient:
    """A client that posts to Home Assistant's webhooks, as the phone does."""
    assert await async_setup_component(hass, "webhook", {})
    return await hass_client_no_auth()
