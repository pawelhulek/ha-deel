"""Entry setup against the fake Deel: sensors, token handling, reauth, diagnostics."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deel.api import DeelClient
from custom_components.deel.const import CONF_TOKEN, CONF_TOKEN_EXPIRES, DOMAIN
from custom_components.deel.diagnostics import async_get_config_entry_diagnostics
from tests.fake_deel import EMAIL, PASSWORD, TOTP_SECRET, DeelState, make_jwt

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ACCOUNT = "sensor.deel_test_worker"
CONTRACT = "sensor.deel_acme_corp"


@pytest.fixture(autouse=True)
def _frozen_time(freezer) -> None:
    freezer.move_to(NOW)


@pytest.fixture
def http() -> MagicMock:
    return MagicMock(detach=MagicMock())


@pytest.fixture
def setup_client(
    http_session: aiohttp.ClientSession, deel_server: str, http: MagicMock
) -> Iterator[DeelClient]:
    client = DeelClient(http_session, base_url=deel_server)
    with patch("custom_components.deel.create_client", return_value=(client, http)):
        yield client


def _entry_with_token(
    base: MockConfigEntry, state: DeelState, valid_for: timedelta
) -> tuple[MockConfigEntry, str]:
    """A copy of ``base`` holding a token the fake accepts, expiring ``valid_for`` from now."""
    expires = NOW + valid_for
    token = make_jwt(expires.timestamp())
    state.valid_tokens.add(token)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=base.title,
        unique_id=base.unique_id,
        data={**base.data, CONF_TOKEN: token, CONF_TOKEN_EXPIRES: expires.isoformat()},
    )
    return entry, token


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _reauth_flows(hass: HomeAssistant) -> list[dict]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


async def test_valid_stored_token_is_reused(
    hass: HomeAssistant,
    setup_client: DeelClient,
    config_entry: MockConfigEntry,
    deel_state: DeelState,
) -> None:
    entry, token = _entry_with_token(config_entry, deel_state, timedelta(hours=20))

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert deel_state.logins == []
    assert setup_client.token == token


async def test_devices(
    hass: HomeAssistant, setup_client: DeelClient, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    assert sorted(d.name for d in devices) == [
        "Deel Acme Corp",
        "Deel Old Client",
        "Deel Test Worker",
    ]


async def test_account_sensors(
    hass: HomeAssistant, setup_client: DeelClient, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    pln = hass.states.get(f"{ACCOUNT}_balance_pln")
    assert float(pln.state) == 4500.0
    assert pln.attributes["unit_of_measurement"] == "PLN"
    total = hass.states.get(f"{ACCOUNT}_total_balance")
    assert float(total.state) == 1234.56
    assert total.attributes["unit_of_measurement"] == "USD"

    withdrawal = hass.states.get(f"{ACCOUNT}_last_withdrawal")
    assert float(withdrawal.state) == 8750.0
    assert withdrawal.attributes["date"] == "2026-10-05T08:30:00+00:00"
    assert withdrawal.attributes["status"] == "sent"
    assert "account_number" not in withdrawal.attributes
    assert hass.states.get(f"{ACCOUNT}_last_withdrawal_date").state == "2026-10-05T08:30:00+00:00"

    upcoming = hass.states.get(f"{ACCOUNT}_upcoming_invoices")
    assert upcoming.state == "1"
    assert upcoming.attributes["invoices"][0]["number"] == "INV-2026-0042"
    due = hass.states.get(f"{ACCOUNT}_next_invoice_due")
    assert due.state == "2026-11-05T00:00:00+00:00"
    assert due.attributes["amount"] == 17500.0

    token = hass.states.get(f"{ACCOUNT}_login_token_expires")
    assert token.state == "2026-10-26T12:00:00+00:00"  # fresh login under frozen time
    assert er.async_get(hass).async_get(token.entity_id).entity_category == "diagnostic"


async def test_time_off_sensors(
    hass: HomeAssistant, setup_client: DeelClient, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    holiday = hass.states.get(f"{ACCOUNT}_next_public_holiday")
    assert holiday.state == "2026-11-01"
    assert holiday.attributes["name"] == "All Saints' Day"
    assert [h["date"] for h in holiday.attributes["upcoming"]] == [
        "2026-11-01",
        "2026-11-11",
        "2026-12-24",
    ]

    paid = hass.states.get(f"{ACCOUNT}_paid_leave_used")
    assert float(paid.state) == 5.0
    assert paid.attributes["unit_of_measurement"] == "d"
    assert paid.attributes["unlimited"] is True
    assert paid.attributes["remaining"] is None

    sick = hass.states.get(f"{ACCOUNT}_sick_leave_used")
    assert float(sick.state) == 3.5
    assert sick.attributes["remaining"] == 6.5


async def test_contract_sensors(
    hass: HomeAssistant, setup_client: DeelClient, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    status = hass.states.get(f"{CONTRACT}_status")
    assert status.state == "in_progress"
    assert status.attributes["job_title"] == "Software Engineer"

    rate = hass.states.get(f"{CONTRACT}_rate")
    assert float(rate.state) == 17500.0
    assert rate.attributes["unit_of_measurement"] == "PLN"
    assert rate.attributes["per"] == "monthly"

    assert hass.states.get(f"{CONTRACT}_payment_cycle_ends").state == "2026-10-31"

    invoice = hass.states.get(f"{CONTRACT}_last_invoice")
    assert float(invoice.state) == 17500.0
    assert invoice.attributes["number"] == "INV-2026-0041"
    assert invoice.attributes["paid"] == "2026-10-04T12:00:00+00:00"

    # Archived contract: status kept, no invoice history fetched.
    assert hass.states.get("sensor.deel_old_client_status").state == "completed"
    assert hass.states.get("sensor.deel_old_client_last_invoice").state == "unknown"


async def test_rejected_token_logs_in_again_and_saves_the_new_one(
    hass: HomeAssistant,
    setup_client: DeelClient,
    config_entry: MockConfigEntry,
    deel_state: DeelState,
) -> None:
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert len(deel_state.logins) == 2  # password, then code
    assert config_entry.data[CONF_TOKEN] in deel_state.valid_tokens
    assert config_entry.data[CONF_TOKEN_EXPIRES] == "2026-10-26T12:00:00+00:00"


async def test_token_close_to_expiry_is_replaced_before_polling(
    hass: HomeAssistant,
    setup_client: DeelClient,
    config_entry: MockConfigEntry,
    deel_state: DeelState,
) -> None:
    entry, soon = _entry_with_token(config_entry, deel_state, timedelta(minutes=5))

    await _setup(hass, entry)

    assert len(deel_state.logins) == 2
    assert entry.data[CONF_TOKEN] != soon
    assert "/deelapi/profiles/me" in deel_state.api_calls


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "AUTHENTICATION_ERROR"), (412, "RECAPTCHA"), (429, "BRUTEFORCE_PROTECTION")],
)
async def test_login_failure_starts_reauth_and_stops_polling(
    hass: HomeAssistant,
    setup_client: DeelClient,
    config_entry: MockConfigEntry,
    deel_state: DeelState,
    status: int,
    code: str,
) -> None:
    deel_state.login_error = (status, code)

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass)) == 1
    assert len(deel_state.logins) == 1


async def test_api_error_retries_setup(
    hass: HomeAssistant,
    setup_client: DeelClient,
    config_entry: MockConfigEntry,
    deel_state: DeelState,
) -> None:
    deel_state.fail_path = ("/deelapi/payments", 503)

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass) == []


async def test_time_off_failure_keeps_payment_sensors(
    hass: HomeAssistant,
    setup_client: DeelClient,
    config_entry: MockConfigEntry,
    deel_state: DeelState,
) -> None:
    deel_state.fail_path = ("/deelapi/time_offs", 500)

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert float(hass.states.get(f"{ACCOUNT}_balance_pln").state) == 4500.0
    assert hass.states.get(f"{ACCOUNT}_next_public_holiday") is None


async def test_time_off_going_away_makes_its_sensors_unavailable(
    hass: HomeAssistant,
    setup_client: DeelClient,
    config_entry: MockConfigEntry,
    deel_state: DeelState,
) -> None:
    await _setup(hass, config_entry)
    deel_state.fail_path = ("/deelapi/time_offs", 500)

    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(f"{ACCOUNT}_next_public_holiday").state == STATE_UNAVAILABLE
    assert hass.states.get(f"{ACCOUNT}_paid_leave_used").state == STATE_UNAVAILABLE


async def test_unload_detaches_http_session(
    hass: HomeAssistant,
    setup_client: DeelClient,
    config_entry: MockConfigEntry,
    http: MagicMock,
) -> None:
    await _setup(hass, config_entry)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    http.detach.assert_called_once()


async def test_diagnostics_are_redacted(
    hass: HomeAssistant, setup_client: DeelClient, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    diag = await async_get_config_entry_diagnostics(hass, config_entry)

    text = repr(diag)
    for secret in (
        EMAIL,
        PASSWORD,
        TOTP_SECRET,
        config_entry.data[CONF_TOKEN],
        "Acme Corp",
        "INV-2026-0041",
        "00000000000000000000000000",
        "REF-SECRET-3",
    ):
        assert secret not in text, secret
    assert diag["entry"]["token_expires"] == "2026-10-26T12:00:00+00:00"
    assert len(diag["contracts"]) == 2
    assert diag["withdrawals"][0]["amount"] == 8750.0
