"""In-process fake of app.deel.com/deelapi, modelled on the real web app (October 2026).

    POST /deelapi/login {email, password}                  -> {totp, otpOptions, ...}
    POST /deelapi/login {totp, email, password: <code>,
                         otpProvider: "app"}               -> {token}
    GET  /deelapi/...   x-auth-token: <token>              -> JSON, 401 without a valid token
    GET  /deelapi/time_offs/...                            -> 400 "Origin not allowed" without
                                                              x-app-host: app.deel.com

Response bodies are synthetic but follow the shapes the real endpoints returned.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from custom_components.deel.totp import code_at

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "worker@example.com"
PASSWORD = "correct-horse"
# RFC 6238 test key ("12345678901234567890").
TOTP_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
CHALLENGE = "challenge-jwt"
HRIS_ID = "b0000000-0000-4000-8000-000000000001"
TIME_OFF_ID = "c0000000-0000-4000-8000-000000000001"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_jwt(exp: float, sub: str = "worker") -> str:
    """An unsigned JWT-shaped token; the client only reads ``exp``."""

    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'sub': sub, 'exp': int(exp)})}.signature"


@dataclass
class DeelState:
    require_2fa: bool = True
    otp_options: list[str] = field(default_factory=lambda: ["app", "email"])
    login_error: tuple[int, str] | None = None  # (status, code) for every login call
    token_lifetime: float = 24 * 3600
    valid_tokens: set[str] = field(default_factory=set)
    logins: list[dict[str, Any]] = field(default_factory=list)
    api_calls: list[str] = field(default_factory=list)
    fail_path: tuple[str, int] | None = None  # (path prefix, status)
    issued: int = 0


def build_app(state: DeelState) -> web.Application:
    def json_response(body: Any, status: int = 200) -> web.Response:
        return web.json_response(body, status=status)

    def new_token() -> str:
        state.issued += 1
        token = make_jwt(time.time() + state.token_lifetime, sub=f"worker-{state.issued}")
        state.valid_tokens.add(token)
        return token

    async def login(request: web.Request) -> web.Response:
        body = await request.json()
        state.logins.append(body)
        if state.login_error:
            status, code = state.login_error
            return json_response({"code": code, "errors": [{"code": code}]}, status)
        if body.get("totp"):
            if body.get("totp") != CHALLENGE or body.get("otpProvider") != "app":
                return json_response({"errors": [{"code": "JWT_ERROR"}]}, 412)
            if body.get("password") != code_at(TOTP_SECRET, time.time()):
                return json_response({"code": "2FA_INVALID_CODE"}, 401)
            return json_response({"token": new_token()})
        if body.get("email") != EMAIL or body.get("password") != PASSWORD:
            return json_response({"code": "AUTHENTICATION_ERROR", "remainingAttempts": 4}, 401)
        if not state.require_2fa:
            return json_response({"token": new_token()})
        return json_response(
            {
                "totp": CHALLENGE,
                "otpOptions": state.otp_options,
                "partialLoginToken": "partial",
                "trustedDeviceFeature": True,
                "hasPhoneNumber": False,
            }
        )

    def api(fixture: str, *, time_off: bool = False):
        async def handler(request: web.Request) -> web.Response:
            state.api_calls.append(request.path)
            if request.headers.get("x-auth-token") not in state.valid_tokens:
                return json_response({"errors": [{"code": "UNAUTHORIZED"}]}, 401)
            if state.fail_path and request.path.startswith(state.fail_path[0]):
                return json_response({"errors": ["failure"]}, state.fail_path[1])
            if time_off and request.headers.get("x-app-host") != "app.deel.com":
                return json_response({"errors": ["Origin not allowed"]}, 400)
            return json_response(load_fixture(fixture))

        return handler

    async def time_off_profile(request: web.Request) -> web.Response:
        if request.query.get("hrisProfileId") != HRIS_ID:
            return json_response({"message": "hrisProfileId is required"}, 400)
        return await api("time_off_profile.json", time_off=True)(request)

    app = web.Application()
    p = "/deelapi"
    app.router.add_post(f"{p}/login", login)
    app.router.add_get(f"{p}/profiles/me", api("profile_me.json"))
    app.router.add_get(f"{p}/contracts/contractors/list", api("contracts.json"))
    app.router.add_get(f"{p}/payments/account/balance", api("balance.json"))
    app.router.add_get(f"{p}/invoices/upcoming", api("invoices_upcoming.json"))
    app.router.add_get(f"{p}/invoices/history", api("invoices_history.json"))
    app.router.add_get(f"{p}/transactions/contractor", api("transactions.json"))
    app.router.add_get(f"{p}/time_offs/profile", time_off_profile)
    app.router.add_get(
        f"{p}/time_offs/profile/{TIME_OFF_ID}/entitlements",
        api("entitlements.json", time_off=True),
    )
    app.router.add_get(
        f"{p}/time_offs/adopter/PROFILE/{TIME_OFF_ID}/upcomingHolidays",
        api("holidays.json", time_off=True),
    )
    return app
