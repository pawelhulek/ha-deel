"""Logs in when needed and polls the worker's Deel data."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CaptchaRequired,
    DeelClient,
    DeelError,
    InvalidAuth,
    InvalidCode,
    LoginBlocked,
    SsoOnly,
    TokenExpired,
    TotpNotEnabled,
    token_expiry,
)
from .const import (
    CONF_TOKEN,
    CONF_TOKEN_EXPIRES,
    CONF_TOTP_SECRET,
    DOMAIN,
    SCAN_INTERVAL,
    TOKEN_REFRESH_MARGIN,
)
from .models import (
    Contract,
    Holiday,
    Invoice,
    Money,
    TimeOffBalance,
    Withdrawal,
    parse_balances,
    parse_contract,
    parse_entitlement,
    parse_holiday,
    parse_invoice,
    parse_withdrawal,
)

if TYPE_CHECKING:
    from . import DeelConfigEntry

_LOGGER = logging.getLogger(__name__)

# Login failures that a retry won't fix: stop polling and ask the user (reauth).
_AUTH_FAILURES: dict[type[DeelError], str] = {
    InvalidAuth: "invalid_auth",
    InvalidCode: "invalid_code",
    TotpNotEnabled: "totp_not_enabled",
    SsoOnly: "sso_only",
    CaptchaRequired: "captcha_required",
    LoginBlocked: "login_blocked",
}


@dataclass(frozen=True)
class TimeOff:
    balances: tuple[TimeOffBalance, ...]
    holidays: tuple[Holiday, ...]


@dataclass(frozen=True)
class DeelData:
    name: str
    balances: tuple[Money, ...]
    total_balance: Money | None
    contracts: MappingProxyType[str, Contract]
    last_invoices: MappingProxyType[str, Invoice]
    upcoming_invoices: tuple[Invoice, ...]
    withdrawals: tuple[Withdrawal, ...]
    time_off: TimeOff | None
    token_expires: datetime | None


def stored_token_expiry(entry: DeelConfigEntry) -> datetime | None:
    raw = entry.data.get(CONF_TOKEN_EXPIRES)
    return dt_util.parse_datetime(raw) if isinstance(raw, str) else None


def auth_failed(err: DeelError) -> ConfigEntryAuthFailed:
    key = next((k for cls, k in _AUTH_FAILURES.items() if isinstance(err, cls)), "invalid_auth")
    return ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key=key)


def _latest(invoices: list[Invoice]) -> Invoice | None:
    dated = [i for i in invoices if i.issued_at is not None]
    return max(dated, key=lambda i: i.issued_at) if dated else None  # type: ignore[arg-type,return-value]


class DeelCoordinator(DataUpdateCoordinator[DeelData]):
    """One account: profile, contracts, balance, invoices, withdrawals and time off."""

    config_entry: DeelConfigEntry

    def __init__(self, hass: HomeAssistant, entry: DeelConfigEntry, client: DeelClient) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL
        )
        self.client = client
        self._time_off_profile_id: str | None = None

    # ---- token --------------------------------------------------------------

    async def _ensure_token(self) -> None:
        expires = stored_token_expiry(self.config_entry)
        token = self.config_entry.data.get(CONF_TOKEN)
        if token and expires and expires - TOKEN_REFRESH_MARGIN > dt_util.utcnow():
            self.client.token = token
            return
        await self._login()

    async def _login(self) -> None:
        data = self.config_entry.data
        try:
            token = await self.client.login(
                data[CONF_EMAIL], data[CONF_PASSWORD], data[CONF_TOTP_SECRET]
            )
        except tuple(_AUTH_FAILURES) as err:
            _LOGGER.warning("Deel login failed: %s", err)
            raise auth_failed(err) from err
        except DeelError as err:
            raise UpdateFailed(f"Deel login failed: {err}") from err
        expires = token_expiry(token)
        _LOGGER.debug("Logged in to Deel; token valid until %s", expires)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **data,
                CONF_TOKEN: token,
                CONF_TOKEN_EXPIRES: expires.isoformat() if expires else None,
            },
        )

    # ---- polling ------------------------------------------------------------

    async def _async_update_data(self) -> DeelData:
        await self._ensure_token()
        try:
            try:
                return await self._fetch()
            except TokenExpired:
                _LOGGER.debug("Deel rejected the token early; logging in again")
                await self._login()
                return await self._fetch()
        except TokenExpired as err:
            raise UpdateFailed("Deel rejected a fresh token") from err
        except DeelError as err:
            raise UpdateFailed(f"Deel API error: {err}") from err

    async def _fetch(self) -> DeelData:
        client = self.client
        me, contracts_raw, balance_raw, upcoming_raw, transactions_raw = await asyncio.gather(
            client.get_me(),
            client.get_contracts(),
            client.get_balance(),
            client.get_upcoming_invoices(),
            client.get_transactions(),
        )
        contracts = [parse_contract(c) for c in contracts_raw if c.get("id")]
        active = [c for c in contracts if not c.archived]
        histories = await asyncio.gather(*(client.get_invoice_history(c.id) for c in active))
        balances, total = parse_balances(balance_raw)
        withdrawals = sorted(
            (w for raw in transactions_raw if (w := parse_withdrawal(raw))),
            key=lambda w: w.date,
            reverse=True,
        )
        last_invoices = {
            contract.id: invoice
            for contract, rows in zip(active, histories, strict=True)
            if (invoice := _latest([parse_invoice(r) for r in rows if r.get("id")]))
        }
        return DeelData(
            name=_display_name(me),
            balances=balances,
            total_balance=total,
            contracts=MappingProxyType({c.id: c for c in contracts}),
            last_invoices=MappingProxyType(last_invoices),
            upcoming_invoices=tuple(
                sorted(
                    (parse_invoice(r) for r in upcoming_raw if r.get("id")),
                    key=lambda i: (i.due_at is None, i.due_at or dt_util.utcnow()),
                )
            ),
            withdrawals=tuple(withdrawals),
            time_off=await self._fetch_time_off(me),
            token_expires=stored_token_expiry(self.config_entry),
        )

    async def _fetch_time_off(self, me: dict[str, Any]) -> TimeOff | None:
        """Time off is a separate Deel service; if it fails, keep the payment data."""
        hris = me.get("hris") if isinstance(me.get("hris"), dict) else {}
        hris_id = hris.get("id")
        if not hris_id:
            _LOGGER.debug("Profile has no HRIS id; skipping time off")
            return None
        try:
            if self._time_off_profile_id is None:
                profile = await self.client.get_time_off_profile(str(hris_id))
                self._time_off_profile_id = str(profile["id"])
            entitlements, holidays = await asyncio.gather(
                self.client.get_entitlements(self._time_off_profile_id),
                self.client.get_upcoming_holidays(self._time_off_profile_id),
            )
        except TokenExpired:
            raise
        except (DeelError, KeyError) as err:
            _LOGGER.warning("Could not read Deel time off: %s", err)
            return None
        today = dt_util.now().date()
        return TimeOff(
            balances=tuple(b for raw in entitlements if (b := parse_entitlement(raw))),
            holidays=tuple(
                sorted(
                    (h for raw in holidays if (h := parse_holiday(raw)) and h.date >= today),
                    key=lambda h: h.date,
                )
            ),
        )


def _display_name(me: dict[str, Any]) -> str:
    for key in ("preferredName", "displayName", "name"):
        if isinstance(value := me.get(key), str) and value.strip():
            return value.strip()
    return "Deel"
