"""Config flow and reauth against the fake Deel."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deel.api import DeelClient
from custom_components.deel.const import CONF_TOKEN, CONF_TOTP_SECRET, DOMAIN
from tests.conftest import PROFILE_ID
from tests.fake_deel import EMAIL, PASSWORD, TOTP_SECRET, DeelState


@pytest.fixture
def http() -> MagicMock:
    return MagicMock(detach=MagicMock())


@pytest.fixture(autouse=True)
def flow_client(
    http_session: aiohttp.ClientSession, deel_server: str, http: MagicMock
) -> Iterator[None]:
    with (
        patch(
            "custom_components.deel.config_flow.create_client",
            side_effect=lambda hass: (DeelClient(http_session, base_url=deel_server), http),
        ),
        patch("custom_components.deel.async_setup_entry", return_value=True),
    ):
        yield


async def _submit(hass: HomeAssistant, **overrides: str) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    values = {"email": f"  {EMAIL.upper()} ", "password": PASSWORD, CONF_TOTP_SECRET: TOTP_SECRET}
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {**values, **overrides}
    )


async def test_creates_entry(hass: HomeAssistant, deel_state: DeelState, http: MagicMock) -> None:
    result = await _submit(
        hass, **{CONF_TOTP_SECRET: TOTP_SECRET.lower()[:8] + " " + TOTP_SECRET[8:]}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Deel (Test Worker)"
    assert result["result"].unique_id == PROFILE_ID
    data = result["data"]
    assert data["email"] == EMAIL
    assert data[CONF_TOTP_SECRET] == TOTP_SECRET
    assert data[CONF_TOKEN] in deel_state.valid_tokens
    assert data["token_expires"] is not None
    http.detach.assert_called_once()


async def test_invalid_secret_is_rejected_before_login(
    hass: HomeAssistant, deel_state: DeelState
) -> None:
    result = await _submit(hass, **{CONF_TOTP_SECRET: "123456"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TOTP_SECRET: "invalid_secret"}
    assert deel_state.logins == []


@pytest.mark.parametrize(
    ("login_error", "error_key"),
    [
        ((401, "AUTHENTICATION_ERROR"), "invalid_auth"),
        ((401, "2FA_INVALID_CODE"), "invalid_code"),
        ((412, "RECAPTCHA"), "captcha_required"),
        ((429, "BRUTEFORCE_PROTECTION"), "login_blocked"),
        ((401, "ACCEPTS_ONLY_SSO_LOGIN"), "sso_only"),
        ((500, ""), "unknown"),
    ],
)
async def test_login_errors_are_shown(
    hass: HomeAssistant,
    deel_state: DeelState,
    http: MagicMock,
    login_error: tuple[int, str],
    error_key: str,
) -> None:
    deel_state.login_error = login_error

    result = await _submit(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error_key}
    http.detach.assert_called_once()


async def test_account_without_authenticator_app(
    hass: HomeAssistant, deel_state: DeelState
) -> None:
    deel_state.otp_options = ["email"]

    result = await _submit(hass)

    assert result["errors"] == {"base": "totp_not_enabled"}


async def test_cannot_connect(hass: HomeAssistant, http_session: aiohttp.ClientSession) -> None:
    with patch(
        "custom_components.deel.config_flow.create_client",
        return_value=(DeelClient(http_session, base_url="http://127.0.0.1:9"), MagicMock()),
    ):
        result = await _submit(hass)

    assert result["errors"] == {"base": "cannot_connect"}


async def test_error_then_success(hass: HomeAssistant, deel_state: DeelState) -> None:
    deel_state.login_error = (401, "AUTHENTICATION_ERROR")
    result = await _submit(hass)
    deel_state.login_error = None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": EMAIL, "password": PASSWORD, CONF_TOTP_SECRET: TOTP_SECRET},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_same_account_twice_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)

    result = await _submit(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_with_stored_details(
    hass: HomeAssistant, config_entry: MockConfigEntry, deel_state: DeelState
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["email"] == EMAIL

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] in deel_state.valid_tokens
    assert config_entry.data["password"] == PASSWORD


async def test_reauth_with_new_password_and_secret(
    hass: HomeAssistant, config_entry: MockConfigEntry, deel_state: DeelState
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=config_entry.unique_id,
        data={**config_entry.data, "password": "old", CONF_TOTP_SECRET: "JBSWY3DPEHPK3PXP"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": PASSWORD, CONF_TOTP_SECRET: f"{TOTP_SECRET.lower()}"}
    )

    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == PASSWORD
    assert entry.data[CONF_TOTP_SECRET] == TOTP_SECRET


async def test_reauth_shows_errors(
    hass: HomeAssistant, config_entry: MockConfigEntry, deel_state: DeelState
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOTP_SECRET: "not a secret!"}
    )
    assert bad["errors"] == {CONF_TOTP_SECRET: "invalid_secret"}

    deel_state.login_error = (412, "RECAPTCHA")
    captcha = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert captcha["errors"] == {"base": "captcha_required"}


async def test_reauth_into_another_account_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="someone-else", data=config_entry.data)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
