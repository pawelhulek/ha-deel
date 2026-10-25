"""Typed views of the Deel responses the sensors use. Everything else is dropped.

Withdrawal rows also carry bank account details; only the fields below are kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.util import dt as dt_util


@dataclass(frozen=True)
class Money:
    amount: float
    currency: str


@dataclass(frozen=True)
class Contract:
    id: str
    name: str
    status: str | None
    contract_type: str | None
    rate: float | None
    currency: str | None
    scale: str | None
    job_title: str | None
    client: str | None
    cycle_start: date | None
    cycle_end: date | None
    archived: bool


@dataclass(frozen=True)
class Invoice:
    id: str
    number: str | None
    status: str | None
    amount: float | None
    currency: str | None
    contract_id: str | None
    client: str | None
    issued_at: datetime | None
    due_at: datetime | None
    paid_at: datetime | None
    overdue: bool


@dataclass(frozen=True)
class Withdrawal:
    id: str
    date: datetime
    amount: float
    currency: str
    status: str | None
    method: str | None


@dataclass(frozen=True)
class TimeOffBalance:
    policy_id: str
    policy: str
    used: float
    approved: float
    requested: float
    entitled: float
    unlimited: bool
    period_end: date | None


@dataclass(frozen=True)
class Holiday:
    name: str
    date: date
    mandatory: bool


def as_float(value: Any) -> float | None:
    """Deel sends amounts as strings ("5250.00") and sometimes as numbers."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None and (day := dt_util.parse_date(value[:10])):
        parsed = datetime(day.year, day.month, day.day)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


def as_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    return dt_util.parse_date(value[:10])


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_balances(raw: dict[str, Any]) -> tuple[tuple[Money, ...], Money | None]:
    """``{"balancesTotal": {...}, "balances": [{"currency", "amount"}]}``."""
    balances = tuple(
        Money(amount, row["currency"])
        for row in raw.get("balances") or []
        if isinstance(row, dict)
        and isinstance(row.get("currency"), str)
        and (amount := as_float(row.get("amount"))) is not None
    )
    total_raw = _dict(raw.get("balancesTotal"))
    total_amount = as_float(total_raw.get("amount"))
    total = (
        Money(total_amount, total_raw["currency"])
        if total_amount is not None and isinstance(total_raw.get("currency"), str)
        else None
    )
    return balances, total


def parse_contract(raw: dict[str, Any]) -> Contract:
    cycle = _dict(raw.get("activePaymentCycle"))
    return Contract(
        id=str(raw["id"]),
        name=_str(raw.get("name")) or str(raw["id"]),
        status=_str(raw.get("status")),
        contract_type=_str(raw.get("contractType")),
        rate=as_float(raw.get("rate")),
        currency=_str(raw.get("currency")),
        scale=_str(raw.get("scale")),
        job_title=_str(raw.get("jobTitleName")),
        client=_str(_dict(raw.get("organization")).get("name")),
        cycle_start=as_date(cycle.get("startPlainDate")),
        cycle_end=as_date(cycle.get("endPlainDate")),
        archived=bool(raw.get("isArchived")),
    )


def parse_invoice(raw: dict[str, Any]) -> Invoice:
    """A row of /invoices/upcoming or /invoices/history (history uses ``total``)."""
    contract = _dict(raw.get("contract"))
    contract_id = contract.get("id") or contract.get("oid")
    amount = as_float(raw.get("total"))
    return Invoice(
        id=str(raw["id"]),
        number=_str(raw.get("invoiceId")) or _str(raw.get("label")),
        status=_str(raw.get("status")),
        amount=amount if amount is not None else as_float(raw.get("amount")),
        currency=_str(raw.get("currency")),
        contract_id=str(contract_id) if contract_id else None,
        client=_str(raw.get("clientName")) or _str(_dict(raw.get("organization")).get("name")),
        issued_at=as_datetime(raw.get("issuedAt")) or as_datetime(raw.get("createdAt")),
        due_at=as_datetime(raw.get("dueDate")),
        paid_at=as_datetime(raw.get("paidAt")),
        overdue=bool(raw.get("isOverdue")),
    )


def parse_withdrawal(raw: dict[str, Any]) -> Withdrawal | None:
    """Only ``type == "withdrawal"`` rows count; the amount is reported as positive."""
    if raw.get("type") != "withdrawal":
        return None
    when = as_datetime(raw.get("date"))
    amount = as_float(raw.get("amount"))
    currency = _str(raw.get("currency"))
    if when is None or amount is None or currency is None:
        return None
    return Withdrawal(
        id=str(raw.get("publicId") or raw.get("id")),
        date=when,
        amount=abs(amount),
        currency=currency,
        status=_str(raw.get("status")),
        method=_str(raw.get("method")),
    )


def parse_entitlement(raw: dict[str, Any]) -> TimeOffBalance | None:
    policy = _dict(raw.get("Policy"))
    if not policy.get("id"):
        return None
    return TimeOffBalance(
        policy_id=str(policy["id"]),
        policy=_str(policy.get("name")) or str(policy["id"]),
        used=as_float(raw.get("used")) or 0.0,
        approved=as_float(raw.get("approved")) or 0.0,
        requested=as_float(raw.get("requested")) or 0.0,
        entitled=as_float(raw.get("totalEntitlements")) or 0.0,
        unlimited="NO_LIMIT" in str(policy.get("balanceTrackingType") or ""),
        period_end=as_date(raw.get("trackingPeriodEndDate")),
    )


def parse_holiday(raw: dict[str, Any]) -> Holiday | None:
    day = as_date(raw.get("formattedDate")) or as_date(raw.get("date"))
    name = _str(raw.get("name"))
    if day is None or name is None:
        return None
    return Holiday(name=name, date=day, mandatory=bool(raw.get("isMandatory")))
