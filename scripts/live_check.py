"""Read-only check of the integration's client against the real Deel, before installing it.

Asks for your e-mail, password and authenticator secret (nothing is echoed or saved), logs in
the way Home Assistant will, reads every endpoint the sensors use and prints a summary without
amounts or names.

    .venv/bin/python scripts/live_check.py
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.deel.api import DeelClient, DeelError, token_expiry  # noqa: E402
from custom_components.deel.models import (  # noqa: E402
    parse_contract,
    parse_entitlement,
    parse_holiday,
    parse_invoice,
    parse_withdrawal,
)
from custom_components.deel.totp import normalize_secret  # noqa: E402


async def check(email: str, password: str, secret: str) -> int:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as http:
        client = DeelClient(http)
        try:
            token = await client.login(email, password, secret)
        except DeelError as err:
            print(f"Login failed: {type(err).__name__}: {err}")
            return 1
        print(f"Logged in; token valid until {token_expiry(token)}")
        try:
            me = await client.get_me()
            contracts = [parse_contract(c) for c in await client.get_contracts()]
            print(f"Profile type: {me.get('profileType')}; contracts: {len(contracts)}")
            for contract in contracts:
                history = await client.get_invoice_history(contract.id)
                statuses = sorted({str(parse_invoice(r).status) for r in history})
                print(
                    f"  contract status={contract.status} type={contract.contract_type} "
                    f"currency={contract.currency} cycle_end={contract.cycle_end} "
                    f"invoices={len(history)} statuses={statuses}"
                )
            balance = await client.get_balance()
            print(f"Balance currencies: {[b.get('currency') for b in balance.get('balances', [])]}")
            upcoming = [parse_invoice(r) for r in await client.get_upcoming_invoices()]
            print(f"Upcoming invoices: {len(upcoming)}; due dates: {[i.due_at for i in upcoming]}")
            withdrawals = [w for r in await client.get_transactions() if (w := parse_withdrawal(r))]
            latest = withdrawals[0].date if withdrawals else None
            print(f"Withdrawals: {len(withdrawals)}; latest: {latest}")
            hris_id = (me.get("hris") or {}).get("id")
            profile = await client.get_time_off_profile(str(hris_id))
            balances = [parse_entitlement(e) for e in await client.get_entitlements(profile["id"])]
            holidays = [parse_holiday(h) for h in await client.get_upcoming_holidays(profile["id"])]
            print(f"Time-off policies: {[b.policy for b in balances if b]}")
            print(f"Holidays: {len(holidays)}; first: {holidays[0] if holidays else None}")
        except DeelError as err:
            print(f"Reading data failed: {type(err).__name__}: {err}")
            return 1
    print("All endpoints answered.")
    return 0


def main() -> int:
    email = input("Deel e-mail: ").strip().lower()
    password = getpass.getpass("Deel password: ")
    secret = normalize_secret(getpass.getpass("Authenticator secret key: "))
    if secret is None:
        print("That is not an authenticator secret key.")
        return 1
    return asyncio.run(check(email, password, secret))


if __name__ == "__main__":
    sys.exit(main())
