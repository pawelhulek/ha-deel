"""Diagnostics download, with credentials, the token and identifying details removed."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import DeelConfigEntry
from .const import CONF_TOKEN, CONF_TOTP_SECRET

TO_REDACT_ENTRY = {CONF_EMAIL, CONF_PASSWORD, CONF_TOTP_SECRET, CONF_TOKEN}
TO_REDACT_DATA = {"id", "name", "number", "client", "job_title", "contract_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DeelConfigEntry
) -> dict[str, Any]:
    data = entry.runtime_data.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT_ENTRY),
        "balances": [asdict(b) for b in data.balances],
        "contracts": [
            async_redact_data(asdict(c), TO_REDACT_DATA) for c in data.contracts.values()
        ],
        "last_invoices": [
            async_redact_data(asdict(i), TO_REDACT_DATA) for i in data.last_invoices.values()
        ],
        "upcoming_invoices": [
            async_redact_data(asdict(i), TO_REDACT_DATA) for i in data.upcoming_invoices
        ],
        "withdrawals": [async_redact_data(asdict(w), TO_REDACT_DATA) for w in data.withdrawals],
        "time_off": asdict(data.time_off) if data.time_off else None,
    }
