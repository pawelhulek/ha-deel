"""Sensors: an account device (balance, withdrawals, invoices, time off) and one per contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DeelConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import DeelCoordinator, DeelData
from .models import Contract, Invoice, Money, TimeOffBalance

APP_URL = "https://app.deel.com"
UPCOMING_LIST_LIMIT = 5


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _invoice_attrs(invoice: Invoice) -> dict[str, Any]:
    return {
        "number": invoice.number,
        "status": invoice.status,
        "amount": invoice.amount,
        "currency": invoice.currency,
        "client": invoice.client,
        "issued": _iso(invoice.issued_at),
        "due": _iso(invoice.due_at),
        "paid": _iso(invoice.paid_at),
        "overdue": invoice.overdue,
    }


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    value_fn: Callable[[DeelData], Any]
    exists_fn: Callable[[DeelData], bool] = lambda _: True
    unit_fn: Callable[[DeelData], str | None] | None = None
    attrs_fn: Callable[[DeelData], dict[str, Any]] | None = None


def _next_holiday(data: DeelData):
    return data.time_off.holidays[0] if data.time_off and data.time_off.holidays else None


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="total_balance",
        translation_key="total_balance",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda d: d.total_balance.amount if d.total_balance else None,
        unit_fn=lambda d: d.total_balance.currency if d.total_balance else None,
        exists_fn=lambda d: d.total_balance is not None,
    ),
    AccountSensorDescription(
        key="last_withdrawal",
        translation_key="last_withdrawal",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda d: d.withdrawals[0].amount if d.withdrawals else None,
        unit_fn=lambda d: d.withdrawals[0].currency if d.withdrawals else None,
        exists_fn=lambda d: bool(d.withdrawals),
        attrs_fn=lambda d: (
            {
                "date": _iso(d.withdrawals[0].date),
                "status": d.withdrawals[0].status,
                "method": d.withdrawals[0].method,
            }
            if d.withdrawals
            else {}
        ),
    ),
    AccountSensorDescription(
        key="last_withdrawal_date",
        translation_key="last_withdrawal_date",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.withdrawals[0].date if d.withdrawals else None,
        exists_fn=lambda d: bool(d.withdrawals),
    ),
    AccountSensorDescription(
        key="upcoming_invoices",
        translation_key="upcoming_invoices",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.upcoming_invoices),
        attrs_fn=lambda d: {
            "overdue": sum(1 for i in d.upcoming_invoices if i.overdue),
            "invoices": [_invoice_attrs(i) for i in d.upcoming_invoices[:UPCOMING_LIST_LIMIT]],
        },
    ),
    AccountSensorDescription(
        key="next_invoice_due",
        translation_key="next_invoice_due",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.upcoming_invoices[0].due_at if d.upcoming_invoices else None,
        attrs_fn=lambda d: _invoice_attrs(d.upcoming_invoices[0]) if d.upcoming_invoices else {},
    ),
    AccountSensorDescription(
        key="next_public_holiday",
        translation_key="next_public_holiday",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: h.date if (h := _next_holiday(d)) else None,
        exists_fn=lambda d: d.time_off is not None,
        attrs_fn=lambda d: {
            "name": h.name if (h := _next_holiday(d)) else None,
            "upcoming": [
                {"date": _iso(h.date), "name": h.name}
                for h in (d.time_off.holidays[:UPCOMING_LIST_LIMIT] if d.time_off else ())
            ],
        },
    ),
    AccountSensorDescription(
        key="token_expiration",
        translation_key="token_expiration",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.token_expires,
    ),
)


def balance_description(money: Money) -> AccountSensorDescription:
    currency = money.currency

    def find(d: DeelData) -> Money | None:
        return next((b for b in d.balances if b.currency == currency), None)

    return AccountSensorDescription(
        key=f"balance_{currency.lower()}",
        translation_key="balance",
        translation_placeholders={"currency": currency},
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=currency,
        suggested_display_precision=2,
        value_fn=lambda d: b.amount if (b := find(d)) else None,
        exists_fn=lambda d: find(d) is not None,
    )


def time_off_description(balance: TimeOffBalance) -> AccountSensorDescription:
    policy_id = balance.policy_id

    def find(d: DeelData) -> TimeOffBalance | None:
        balances = d.time_off.balances if d.time_off else ()
        return next((b for b in balances if b.policy_id == policy_id), None)

    return AccountSensorDescription(
        key=f"time_off_used_{policy_id}",
        translation_key="time_off_used",
        translation_placeholders={"policy": balance.policy},
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: b.used if (b := find(d)) else None,
        exists_fn=lambda d: find(d) is not None,
        attrs_fn=lambda d: (
            {
                "approved": b.approved,
                "requested": b.requested,
                "entitled": None if b.unlimited else b.entitled,
                "remaining": None if b.unlimited else round(b.entitled - b.used - b.approved, 2),
                "unlimited": b.unlimited,
                "period_end": _iso(b.period_end),
            }
            if (b := find(d))
            else {}
        ),
    )


@dataclass(frozen=True, kw_only=True)
class ContractSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Contract, Invoice | None], Any]
    exists_fn: Callable[[Contract], bool] = lambda _: True
    unit_fn: Callable[[Contract, Invoice | None], str | None] | None = None
    attrs_fn: Callable[[Contract, Invoice | None], dict[str, Any]] | None = None


CONTRACT_SENSORS: tuple[ContractSensorDescription, ...] = (
    ContractSensorDescription(
        key="status",
        translation_key="contract_status",
        value_fn=lambda c, _: c.status,
        attrs_fn=lambda c, _: {
            "type": c.contract_type,
            "job_title": c.job_title,
            "client": c.client,
            "archived": c.archived,
        },
    ),
    ContractSensorDescription(
        key="rate",
        translation_key="rate",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda c, _: c.rate,
        unit_fn=lambda c, _: c.currency,
        exists_fn=lambda c: c.rate is not None and c.currency is not None,
        attrs_fn=lambda c, _: {"per": c.scale},
    ),
    ContractSensorDescription(
        key="cycle_end",
        translation_key="cycle_end",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c, _: c.cycle_end,
        exists_fn=lambda c: c.cycle_end is not None,
        attrs_fn=lambda c, _: {"cycle_start": _iso(c.cycle_start)},
    ),
    ContractSensorDescription(
        key="last_invoice",
        translation_key="last_invoice",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda _, i: i.amount if i else None,
        unit_fn=lambda c, i: (i.currency if i else None) or c.currency,
        attrs_fn=lambda _, i: _invoice_attrs(i) if i else {},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new_entities() -> None:
        data = coordinator.data
        candidates: list[SensorEntity] = [
            AccountSensor(coordinator, d)
            for d in (
                *ACCOUNT_SENSORS,
                *(balance_description(b) for b in data.balances),
                *(
                    time_off_description(b)
                    for b in (data.time_off.balances if data.time_off else ())
                ),
            )
            if d.exists_fn(data)
        ]
        candidates += [
            ContractSensor(coordinator, contract, d)
            for contract in data.contracts.values()
            for d in CONTRACT_SENSORS
            if d.exists_fn(contract)
        ]
        new = [e for e in candidates if e.unique_id not in known]
        known.update(e.unique_id for e in new if e.unique_id)
        if new:
            async_add_entities(new)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class AccountSensor(CoordinatorEntity[DeelCoordinator], SensorEntity):
    """A value of the whole Deel account."""

    _attr_has_entity_name = True
    entity_description: AccountSensorDescription

    def __init__(self, coordinator: DeelCoordinator, description: AccountSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name=f"{MANUFACTURER} {coordinator.data.name}",
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=APP_URL,
        )

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.exists_fn(self.coordinator.data)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.unit_fn is not None:
            return self.entity_description.unit_fn(self.coordinator.data)
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class ContractSensor(CoordinatorEntity[DeelCoordinator], SensorEntity):
    """A value of one contract."""

    _attr_has_entity_name = True
    entity_description: ContractSensorDescription

    def __init__(
        self,
        coordinator: DeelCoordinator,
        contract: Contract,
        description: ContractSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._contract_id = contract.id
        self._attr_unique_id = f"{contract.id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, contract.id)},
            name=f"{MANUFACTURER} {contract.client or contract.name}",
            manufacturer=MANUFACTURER,
            model=contract.job_title or contract.contract_type,
            configuration_url=f"{APP_URL}/contract/{contract.id}/details",
            via_device=(DOMAIN, str(coordinator.config_entry.unique_id)),
        )

    @property
    def _contract(self) -> Contract | None:
        return self.coordinator.data.contracts.get(self._contract_id)

    @property
    def _invoice(self) -> Invoice | None:
        return self.coordinator.data.last_invoices.get(self._contract_id)

    @property
    def available(self) -> bool:
        contract = self._contract
        return (
            super().available
            and contract is not None
            and self.entity_description.exists_fn(contract)
        )

    @property
    def native_value(self) -> Any:
        contract = self._contract
        return self.entity_description.value_fn(contract, self._invoice) if contract else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        contract = self._contract
        if self.entity_description.unit_fn is not None and contract is not None:
            return self.entity_description.unit_fn(contract, self._invoice)
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        contract = self._contract
        if contract is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(contract, self._invoice)
