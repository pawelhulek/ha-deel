"""Client for the Deel web app's own API (app.deel.com/deelapi), as a worker sees it.

Deel's public API only gives tokens to client organizations, so this talks to the same
endpoints the web app uses. Observed in October 2026:

* ``POST /deelapi/login`` with e-mail + password returns ``{"token"}``, or, with 2FA on,
  ``{"totp", "otpOptions", ...}``. The code then goes to ``POST /deelapi/login`` again as
  ``{"totp", "email", "password": <code>, "otpProvider": "app"}``.
* The token is an RS256 JWT valid for 24 hours. It is never extended; the web app logs in again.
* Every call sends the token in ``x-auth-token``. Time-off endpoints also need
  ``x-app-host: app.deel.com`` and answer "Origin not allowed" without it.
* Login can answer ``412 RECAPTCHA``. The web app then solves a captcha; this client does not.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import aiohttp
from yarl import URL

from .const import APP_HOST, BASE_URL
from .totp import code_at, seconds_left

_LOGGER = logging.getLogger(__name__)

API_PREFIX = "/deelapi"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
USER_AGENT = "HomeAssistant-Deel/0.1"
# Don't submit a code that expires before the request arrives.
MIN_CODE_SECONDS_LEFT = 3.0
TOTP_PROVIDER = "app"


class DeelError(Exception):
    """Base error."""


class CannotConnect(DeelError):
    """Network problem or timeout."""


class InvalidAuth(DeelError):
    """Wrong e-mail or password."""


class InvalidCode(DeelError):
    """The authenticator code was rejected (wrong secret or a wrong clock)."""


class TotpNotEnabled(DeelError):
    """The account's 2FA is not an authenticator app, so HA cannot produce the code."""


class SsoOnly(DeelError):
    """The account or organization only allows single sign-on."""


class CaptchaRequired(DeelError):
    """Deel wants a captcha solved before it lets this login through."""


class LoginBlocked(DeelError):
    """Too many attempts, or the account/region is blocked."""


class TokenExpired(DeelError):
    """The token is missing, expired or was revoked."""


class UnexpectedResponse(DeelError):
    """Deel answered with something this client does not understand."""


_LOGIN_ERRORS: dict[str, type[DeelError]] = {
    "AUTHENTICATION_ERROR": InvalidAuth,
    "NOT_INVITED_YET": InvalidAuth,
    "INVITED_BUT_NOT_ACCEPTED_YET": InvalidAuth,
    "2FA_INVALID_CODE": InvalidCode,
    "2FA_TOO_MANY_FAILED_ATTEMPTS": LoginBlocked,
    "BLOCKED": LoginBlocked,
    "REGION_BLOCKED": LoginBlocked,
    "BRUTEFORCE_PROTECTION": LoginBlocked,
    "ACCEPTS_ONLY_SSO_LOGIN": SsoOnly,
    "ORGANIZATION_SSO_ENABLED": SsoOnly,
    "SSO_USER_NOT_LINKED_TO_SINGLE_ORG": SsoOnly,
    "RECAPTCHA": CaptchaRequired,
}


def token_expiry(token: str) -> datetime | None:
    """The ``exp`` claim of a JWT, read without verifying the signature."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.fromtimestamp(int(claims["exp"]), UTC)
    except IndexError, KeyError, TypeError, ValueError, binascii.Error:
        return None


def _error_code(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    code = body.get("code")
    errors = body.get("errors")
    if not code and isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code = errors[0].get("code")
    return str(code or "").upper()


class DeelClient:
    """Async client. One instance per account; holds the current token."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = BASE_URL,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._session = session
        self._base = URL(base_url)
        self._clock = clock
        self.token: str | None = None

    # ---- login --------------------------------------------------------------

    async def login(self, email: str, password: str, totp_secret: str) -> str:
        """Log in and answer the authenticator-app challenge. Returns and keeps the token."""
        body = await self._post_login({"email": email, "password": password})
        if not (token := body.get("token")):
            challenge = body.get("totp")
            options = body.get("otpOptions") or []
            if not challenge or TOTP_PROVIDER not in options:
                raise TotpNotEnabled(f"2FA options offered: {options or 'none'}")
            code = await self._fresh_code(totp_secret)
            body = await self._post_login(
                {
                    "totp": challenge,
                    "trusted": False,
                    "email": email,
                    "password": code,
                    "otpProvider": TOTP_PROVIDER,
                }
            )
            token = body.get("token")
        if not isinstance(token, str) or not token:
            raise UnexpectedResponse("Login succeeded without a token")
        self.token = token
        return token

    def _now(self) -> float:
        return self._clock() if self._clock else time.time()

    async def _fresh_code(self, secret: str) -> str:
        now = self._now()
        if (left := seconds_left(now)) < MIN_CODE_SECONDS_LEFT:
            await asyncio.sleep(left + 0.5)
            now = self._now()
        return code_at(secret, now)

    async def _post_login(self, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = await self._request("POST", "/login", json_body=payload)
        if status == 200 and isinstance(body, dict):
            return body
        code = _error_code(body)
        error = _LOGIN_ERRORS.get(code)
        if error is None and status == 400:
            error = InvalidAuth
        if error is None and status == 429:
            error = LoginBlocked
        raise (error or UnexpectedResponse)(f"Login answered HTTP {status} {code}".strip())

    # ---- data ---------------------------------------------------------------

    async def get_me(self) -> dict[str, Any]:
        return await self._get_object("/profiles/me")

    async def get_contracts(self) -> list[dict[str, Any]]:
        body = await self._get_object("/contracts/contractors/list", {"limit": "50"})
        return _rows(body, "rows")

    async def get_balance(self) -> dict[str, Any]:
        return await self._get_object("/payments/account/balance")

    async def get_upcoming_invoices(self) -> list[dict[str, Any]]:
        return _rows(await self._get_object("/invoices/upcoming"), "rows")

    async def get_invoice_history(self, contract_id: str) -> list[dict[str, Any]]:
        params = {
            "contractId": contract_id,
            "useCursor": "true",
            "includeFilterValues": "false",
            "includeShielded": "false",
            "excludeTypes[]": "CREDIT_NOTE",
        }
        return _rows(await self._get_object("/invoices/history", params), "rows")

    async def get_transactions(self) -> list[dict[str, Any]]:
        return _rows(await self._get_object("/transactions/contractor"), "transactions")

    async def get_time_off_profile(self, hris_profile_id: str) -> dict[str, Any]:
        body = await self._get_object("/time_offs/profile", {"hrisProfileId": hris_profile_id})
        profile = body.get("profile")
        if not isinstance(profile, dict):
            raise UnexpectedResponse("Time-off profile response has no 'profile'")
        return profile

    async def get_entitlements(self, time_off_profile_id: str) -> list[dict[str, Any]]:
        body = await self._get_object(f"/time_offs/profile/{time_off_profile_id}/entitlements")
        return _rows(body, "entitlements")

    async def get_upcoming_holidays(self, time_off_profile_id: str) -> list[dict[str, Any]]:
        path = f"/time_offs/adopter/PROFILE/{time_off_profile_id}/upcomingHolidays"
        body = await self._get_json(path)
        if not isinstance(body, list):
            raise UnexpectedResponse("Holidays response is not a list")
        return [row for row in body if isinstance(row, dict)]

    async def _get_object(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        body = await self._get_json(path, params)
        if not isinstance(body, dict):
            raise UnexpectedResponse(f"Unexpected JSON shape from {path}")
        return body

    async def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        if not self.token:
            raise TokenExpired("Not logged in")
        status, body = await self._request("GET", path, params=params)
        if status == 401:
            raise TokenExpired(f"HTTP 401 from {path}")
        if status != 200:
            raise UnexpectedResponse(f"HTTP {status} {_error_code(body)} from {path}".strip())
        return body

    # ---- transport ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "x-app-host": APP_HOST,
            "x-platform": "web",
        }
        if self.token and json_body is None:
            headers["x-auth-token"] = self.token
        url = self._base.with_path(API_PREFIX + path)
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(f"{method} {path} failed: {err}") from err
        try:
            return status, json.loads(text) if text else None
        except ValueError as err:
            if status >= 500:
                raise CannotConnect(f"HTTP {status} from {path}") from err
            raise UnexpectedResponse(f"Non-JSON response (HTTP {status}) from {path}") from err


def _rows(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = body.get(key)
    if not isinstance(rows, list):
        raise UnexpectedResponse(f"Response has no '{key}' list")
    return [row for row in rows if isinstance(row, dict)]
