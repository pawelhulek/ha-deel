"""DeelClient against the in-process fake of app.deel.com."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from custom_components.deel.api import (
    CannotConnect,
    CaptchaRequired,
    DeelClient,
    InvalidAuth,
    InvalidCode,
    LoginBlocked,
    SsoOnly,
    TokenExpired,
    TotpNotEnabled,
    UnexpectedResponse,
    token_expiry,
)
from custom_components.deel.totp import code_at
from tests.fake_deel import (
    EMAIL,
    HRIS_ID,
    PASSWORD,
    TIME_OFF_ID,
    TOTP_SECRET,
    DeelState,
    make_jwt,
)


@pytest.fixture
def client(http_session: aiohttp.ClientSession, deel_server: str) -> DeelClient:
    return DeelClient(http_session, base_url=deel_server)


async def test_login_answers_the_authenticator_challenge(
    client: DeelClient, deel_state: DeelState
) -> None:
    token = await client.login(EMAIL, PASSWORD, TOTP_SECRET)

    assert token in deel_state.valid_tokens
    assert client.token == token
    first, second = deel_state.logins
    assert first == {"email": EMAIL, "password": PASSWORD}
    assert second["totp"] == "challenge-jwt"
    assert second["otpProvider"] == "app"
    assert second["trusted"] is False
    assert second["password"].isdigit() and len(second["password"]) == 6


async def test_login_without_2fa(client: DeelClient, deel_state: DeelState) -> None:
    deel_state.require_2fa = False

    assert await client.login(EMAIL, PASSWORD, TOTP_SECRET) in deel_state.valid_tokens
    assert len(deel_state.logins) == 1


async def test_wrong_password(client: DeelClient) -> None:
    with pytest.raises(InvalidAuth):
        await client.login(EMAIL, "wrong", TOTP_SECRET)


async def test_wrong_secret_is_an_invalid_code(client: DeelClient) -> None:
    with pytest.raises(InvalidCode):
        await client.login(EMAIL, PASSWORD, "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")


async def test_account_without_authenticator_app(client: DeelClient, deel_state: DeelState) -> None:
    deel_state.otp_options = ["email", "sms"]

    with pytest.raises(TotpNotEnabled):
        await client.login(EMAIL, PASSWORD, TOTP_SECRET)
    assert len(deel_state.logins) == 1


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (412, "RECAPTCHA", CaptchaRequired),
        (401, "2FA_TOO_MANY_FAILED_ATTEMPTS", LoginBlocked),
        (401, "BLOCKED", LoginBlocked),
        (429, "BRUTEFORCE_PROTECTION", LoginBlocked),
        (429, "", LoginBlocked),
        (401, "ACCEPTS_ONLY_SSO_LOGIN", SsoOnly),
        (400, "", InvalidAuth),
        (500, "", UnexpectedResponse),
    ],
)
async def test_login_errors(
    client: DeelClient,
    deel_state: DeelState,
    status: int,
    code: str,
    error: type[Exception],
) -> None:
    deel_state.login_error = (status, code)

    with pytest.raises(error):
        await client.login(EMAIL, PASSWORD, TOTP_SECRET)


async def test_code_about_to_expire_waits_for_the_next_one(
    http_session: aiohttp.ClientSession, deel_server: str, deel_state: DeelState
) -> None:
    times = iter([89.0, 91.0])
    client = DeelClient(http_session, base_url=deel_server, clock=lambda: next(times))
    with (
        patch("custom_components.deel.api.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(InvalidCode),  # the fake checks against the real clock
    ):
        await client.login(EMAIL, PASSWORD, TOTP_SECRET)

    sleep.assert_awaited_once_with(1.5)
    assert deel_state.logins[1]["password"] == code_at(TOTP_SECRET, 91.0)


async def test_data_endpoints(client: DeelClient, deel_state: DeelState) -> None:
    await client.login(EMAIL, PASSWORD, TOTP_SECRET)

    assert (await client.get_me())["hris"]["id"] == HRIS_ID
    assert [c["id"] for c in await client.get_contracts()] == ["abc1234", "old9876"]
    assert (await client.get_balance())["balances"][0]["currency"] == "PLN"
    assert len(await client.get_upcoming_invoices()) == 1
    assert len(await client.get_invoice_history("abc1234")) == 2
    assert len(await client.get_transactions()) == 3
    profile = await client.get_time_off_profile(HRIS_ID)
    assert profile["id"] == TIME_OFF_ID
    assert len(await client.get_entitlements(TIME_OFF_ID)) == 2
    assert len(await client.get_upcoming_holidays(TIME_OFF_ID)) == 4


async def test_unknown_token_is_expired(client: DeelClient) -> None:
    client.token = "revoked"

    with pytest.raises(TokenExpired):
        await client.get_me()


async def test_no_token_is_expired(client: DeelClient, deel_state: DeelState) -> None:
    with pytest.raises(TokenExpired):
        await client.get_contracts()
    assert deel_state.api_calls == []


async def test_server_error_is_unexpected(client: DeelClient, deel_state: DeelState) -> None:
    await client.login(EMAIL, PASSWORD, TOTP_SECRET)
    deel_state.fail_path = ("/deelapi/invoices", 503)

    with pytest.raises(UnexpectedResponse):
        await client.get_upcoming_invoices()


async def test_wrong_shape_is_unexpected(client: DeelClient) -> None:
    await client.login(EMAIL, PASSWORD, TOTP_SECRET)

    # Holidays come back as a list; reading them as an object must fail cleanly.
    with pytest.raises(UnexpectedResponse):
        await client._get_object(f"/time_offs/adopter/PROFILE/{TIME_OFF_ID}/upcomingHolidays")


async def test_connection_error(http_session: aiohttp.ClientSession) -> None:
    client = DeelClient(http_session, base_url="http://127.0.0.1:9")

    with pytest.raises(CannotConnect):
        await client.login(EMAIL, PASSWORD, TOTP_SECRET)


def test_token_expiry_reads_exp() -> None:
    assert token_expiry(make_jwt(1_800_000_000)) == datetime.fromtimestamp(1_800_000_000, UTC)


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "a.e30.c"])
def test_token_expiry_of_garbage(token: str) -> None:
    assert token_expiry(token) is None
