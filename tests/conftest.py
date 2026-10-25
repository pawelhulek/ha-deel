"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deel.const import (
    CONF_TOKEN,
    CONF_TOKEN_EXPIRES,
    CONF_TOTP_SECRET,
    DOMAIN,
)
from tests.fake_deel import EMAIL, PASSWORD, TOTP_SECRET, DeelState, build_app

pytest_plugins = ["pytest_homeassistant_custom_component"]

PROFILE_ID = "a0000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load custom_components/ in every test."""
    return


@pytest.fixture
def deel_state() -> DeelState:
    return DeelState()


@pytest.fixture
async def deel_server(aiohttp_server, deel_state: DeelState, socket_enabled):
    """Running fake Deel; yields its base URL."""
    # No access log: aiohttp's log formatter breaks under frozen time.
    server = await aiohttp_server(build_app(deel_state), access_log=None)
    return str(server.make_url("")).rstrip("/")


@pytest.fixture
async def http_session(socket_enabled) -> AsyncIterator[aiohttp.ClientSession]:
    session = aiohttp.ClientSession()
    yield session
    await session.close()


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Deel (Test Worker)",
        unique_id=PROFILE_ID,
        data={
            "email": EMAIL,
            "password": PASSWORD,
            CONF_TOTP_SECRET: TOTP_SECRET,
            CONF_TOKEN: "stored-token",
            CONF_TOKEN_EXPIRES: "2026-10-26T12:00:00+00:00",
        },
    )
