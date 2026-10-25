"""Parsing of Deel responses, including the odd shapes."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from custom_components.deel.models import (
    as_datetime,
    as_float,
    parse_balances,
    parse_contract,
    parse_entitlement,
    parse_holiday,
    parse_invoice,
    parse_withdrawal,
)
from tests.fake_deel import load_fixture


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5250.00", 5250.0),
        (17500, 17500.0),
        ("-8.5", -8.5),
        ("", None),
        (None, None),
        ("abc", None),
        (True, None),
    ],
)
def test_as_float(raw: object, expected: float | None) -> None:
    assert as_float(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-05T08:30:00.000Z", datetime(2026, 10, 5, 8, 30, tzinfo=UTC)),
        ("2026-01-01T00:00:00Z", datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-11-05", datetime(2026, 11, 5, tzinfo=UTC)),
        ("2026-11-05T10:00:00", datetime(2026, 11, 5, 10, tzinfo=UTC)),
        ("soon", None),
        (None, None),
    ],
)
def test_as_datetime(raw: object, expected: datetime | None) -> None:
    assert as_datetime(raw) == expected


def test_balances() -> None:
    balances, total = parse_balances(load_fixture("balance.json"))

    assert [(b.currency, b.amount) for b in balances] == [("PLN", 4500.0)]
    assert (total.currency, total.amount) == ("USD", 1234.56)


def test_balances_tolerate_missing_parts() -> None:
    balances, total = parse_balances(
        {"balances": [{"currency": "EUR"}, "junk", {"amount": "1"}], "balancesTotal": None}
    )

    assert balances == ()
    assert total is None


def test_contract() -> None:
    active, archived = (parse_contract(c) for c in load_fixture("contracts.json")["rows"])

    assert active.client == "Acme Corp"
    assert active.rate == 17500.0
    assert active.cycle_end == date(2026, 10, 31)
    assert not active.archived
    assert archived.archived
    assert archived.cycle_end is None


def test_invoice_prefers_total_over_amount() -> None:
    invoice = parse_invoice({"id": 1, "amount": "100.00", "total": "123.00"})

    assert invoice.amount == 123.0


def test_upcoming_invoice() -> None:
    invoice = parse_invoice(load_fixture("invoices_upcoming.json")["rows"][0])

    assert invoice.number == "INV-2026-0042"
    assert invoice.amount == 17500.0
    assert invoice.contract_id == "abc1234"
    assert invoice.due_at == datetime(2026, 11, 5, tzinfo=UTC)
    assert invoice.paid_at is None


def test_withdrawals_keep_no_bank_details() -> None:
    rows = load_fixture("transactions.json")["transactions"]
    parsed = [parse_withdrawal(r) for r in rows]

    assert parsed[2] is None  # a payment, not a withdrawal
    first = parsed[0]
    assert first.amount == 8750.0
    assert first.status == "sent"
    assert "0000000000" not in repr(first)
    assert "REF-SECRET" not in repr(first)


def test_withdrawal_without_date_is_skipped() -> None:
    assert parse_withdrawal({"type": "withdrawal", "amount": "1", "currency": "PLN"}) is None


def test_entitlements() -> None:
    paid, sick = (parse_entitlement(e) for e in load_fixture("entitlements.json")["entitlements"])

    assert (paid.policy, paid.used, paid.approved, paid.unlimited) == ("Paid leave", 5.0, 2.0, True)
    assert (sick.entitled, sick.unlimited, sick.period_end) == (10.0, False, date(2026, 12, 31))
    assert parse_entitlement({"Policy": {}}) is None


def test_holidays() -> None:
    holiday = parse_holiday(load_fixture("holidays.json")[1])

    assert (holiday.name, holiday.date, holiday.mandatory) == (
        "Christmas Eve",
        date(2026, 12, 24),
        True,
    )
    assert parse_holiday({"name": "No date"}) is None
