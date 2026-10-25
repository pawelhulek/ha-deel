"""Config flow: e-mail, password and the authenticator-app secret; reauth with the same."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import (
    CannotConnect,
    CaptchaRequired,
    InvalidAuth,
    InvalidCode,
    LoginBlocked,
    SsoOnly,
    TotpNotEnabled,
    token_expiry,
)
from .const import CONF_TOKEN, CONF_TOKEN_EXPIRES, CONF_TOTP_SECRET, DOMAIN
from .session import create_client
from .totp import normalize_secret

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
_SECRET_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
        vol.Required(CONF_TOTP_SECRET): _SECRET_SELECTOR,
    }
)
REAUTH_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PASSWORD): _PASSWORD_SELECTOR,
        vol.Optional(CONF_TOTP_SECRET): _SECRET_SELECTOR,
    }
)

_ERROR_KEYS: tuple[tuple[type[Exception], str], ...] = (
    (InvalidAuth, "invalid_auth"),
    (InvalidCode, "invalid_code"),
    (TotpNotEnabled, "totp_not_enabled"),
    (SsoOnly, "sso_only"),
    (CaptchaRequired, "captcha_required"),
    (LoginBlocked, "login_blocked"),
    (CannotConnect, "cannot_connect"),
)


def _error_key(err: Exception) -> str:
    for cls, key in _ERROR_KEYS:
        if isinstance(err, cls):
            return key
    _LOGGER.exception("Unexpected error during Deel login")
    return "unknown"


class DeelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Deel."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            secret = normalize_secret(user_input[CONF_TOTP_SECRET])
            if secret is None:
                errors[CONF_TOTP_SECRET] = "invalid_secret"
            else:
                try:
                    profile_id, title, token = await self._login(
                        email, user_input[CONF_PASSWORD], secret
                    )
                except Exception as err:  # noqa: BLE001 - mapped to a form error
                    errors["base"] = _error_key(err)
                else:
                    await self.async_set_unique_id(profile_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=title,
                        data=self._entry_data(email, user_input[CONF_PASSWORD], secret, token),
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {k: v for k, v in (user_input or {}).items() if k == CONF_EMAIL}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Login stopped working (password, secret, captcha or a lockout)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]
            raw_secret = user_input.get(CONF_TOTP_SECRET)
            secret = normalize_secret(raw_secret) if raw_secret else entry.data[CONF_TOTP_SECRET]
            if secret is None:
                errors[CONF_TOTP_SECRET] = "invalid_secret"
            else:
                try:
                    profile_id, _, token = await self._login(
                        entry.data[CONF_EMAIL], password, secret
                    )
                except Exception as err:  # noqa: BLE001
                    errors["base"] = _error_key(err)
                else:
                    await self.async_set_unique_id(profile_id)
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates=self._entry_data(
                            entry.data[CONF_EMAIL], password, secret, token
                        ),
                    )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
        )

    async def _login(self, email: str, password: str, secret: str) -> tuple[str, str, str]:
        """Log in and read the profile. Returns (profile id, entry title, token)."""
        client, http = create_client(self.hass)
        try:
            token = await client.login(email, password, secret)
            me = await client.get_me()
        finally:
            # HA-created sessions share HA's connector: detach, never close.
            http.detach()
        profile_id = me.get("id") or me.get("profilePublicId")
        if not profile_id:
            raise InvalidAuth("Profile has no id")
        return str(profile_id), _title(me, email), token

    @staticmethod
    def _entry_data(email: str, password: str, secret: str, token: str) -> dict[str, Any]:
        expires = token_expiry(token)
        return {
            CONF_EMAIL: email,
            CONF_PASSWORD: password,
            CONF_TOTP_SECRET: secret,
            CONF_TOKEN: token,
            CONF_TOKEN_EXPIRES: expires.isoformat() if expires else None,
        }


def _title(me: dict[str, Any], email: str) -> str:
    name = me.get("preferredName") or me.get("displayName") or me.get("name")
    return f"Deel ({name})" if isinstance(name, str) and name.strip() else f"Deel ({email})"
