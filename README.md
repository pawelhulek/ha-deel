# Deel for Home Assistant (worker side)

Custom integration that shows what a contractor or employee sees on [Deel](https://app.deel.com):
balance, withdrawals, invoices, contracts, time off and public holidays.

Deel's [public API](https://developer.deel.com) only issues tokens to client organizations, so this
integration logs in the way the Deel web app does and reads the web app's own endpoints
(`app.deel.com/deelapi`). Those endpoints are undocumented and can change without notice.

## What you get

An account device, **Deel _your name_**:

| Sensor | Example | Notes |
| --- | --- | --- |
| Balance _PLN_ | `4500.00 PLN` | One per wallet currency |
| Total balance | `1234.56 USD` | Deel's total across currencies |
| Last withdrawal | `8750.00 PLN` | Attributes: `date`, `status`, `method` |
| Last withdrawal date | timestamp | |
| Upcoming invoices | `1` | Attributes: `overdue`, `invoices` (next 5) |
| Next invoice due | timestamp | Attributes: number, amount, client, status |
| Next public holiday | date | Attributes: `name`, `upcoming` (next 5) |
| _Policy_ used | `5 d` | One per time-off policy. Attributes: `approved`, `requested`, `remaining` |
| Login token expires | timestamp | Diagnostic |

One device per contract, **Deel _client_**:

| Sensor | Example | Notes |
| --- | --- | --- |
| Status | `in_progress` | Attributes: `type`, `job_title`, `client`, `archived` |
| Rate | `17500 PLN` | Attribute `per`: `monthly`, `hourly`, … |
| Payment cycle ends | date | |
| Last invoice | `17500.00 PLN` | Attributes: number, status, issued, due, paid |

Data is polled every hour. Deel's task list is not included: its endpoint and format could not be
observed.

## Before you set it up: the authenticator secret

Deel requires two-step verification, and its login token lasts only **24 hours**. To stay logged
in, Home Assistant logs in again every day and answers the 2FA challenge itself. For that it needs
the **secret key** of an authenticator app (TOTP), not a 6-digit code.

1. In Deel's security settings, set up the authenticator app again (or for the first time, if you
   use e-mail or SMS codes now).
2. Where Deel shows the QR code, choose to see the key as text. It looks like
   `JBSW Y3DP EHPK 3PXP …`.
3. Add that key to your phone's authenticator app as well, and finish the setup in Deel.
4. Keep the key for the Home Assistant setup.

Anyone with this key and your password can log in to your Deel account. Home Assistant stores
both in its config entry, the same way it stores other integrations' passwords.

Google sign-in and SSO accounts are not supported.

## Install (manual)

Copy `custom_components/deel` into the `custom_components` folder of your Home Assistant config
directory and restart Home Assistant.

## Set up

*Settings → Devices & services → Add integration → Deel*, then enter your e-mail, password and the
authenticator secret key (an `otpauth://` link also works).

## When login stops working

If Deel rejects the password or the generated code, or asks for a **captcha**, or locks the login
after too many attempts, polling stops and Home Assistant shows a *Re-authentication required*
notification. The integration never tries to solve captchas. Log in once at app.deel.com in a
browser, wait a while, then open the notification and press **Submit**. Change the password or
secret there only if they changed.

A rejected code usually means a wrong secret or a wrong Home Assistant clock.

## Privacy

The integration only talks to `app.deel.com`. Withdrawal rows from Deel include bank account
details; the integration drops them when it parses the response. Diagnostics downloads have the
e-mail, password, secret, token, names, contract and invoice ids removed.

## How it talks to Deel

Observed in the web app in October 2026:

- `POST /deelapi/login` `{email, password}` → `{token}` or `{totp, otpOptions, …}`.
  The code goes to `POST /deelapi/login` `{totp, email, password: <code>, otpProvider: "app"}`.
  Errors carry a `code`: `AUTHENTICATION_ERROR`, `2FA_INVALID_CODE`, `RECAPTCHA` (412),
  `BRUTEFORCE_PROTECTION` (429), and so on.
- Other calls send the token in `x-auth-token`. Time-off calls also need
  `x-app-host: app.deel.com`.
- Endpoints: `profiles/me`, `contracts/contractors/list`, `payments/account/balance`,
  `invoices/upcoming`, `invoices/history?contractId=…`, `transactions/contractor`,
  `time_offs/profile?hrisProfileId=…`, `time_offs/profile/{id}/entitlements`,
  `time_offs/adopter/PROFILE/{id}/upcomingHolidays`.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python pytest-homeassistant-custom-component pytest-cov ruff
.venv/bin/python -m pytest --cov --cov-report=term-missing
```

The tests run the client against an in-process fake of Deel (`tests/fake_deel.py`). Its responses
(`tests/fixtures/`) are synthetic, shaped after the real ones.

`scripts/live_check.py` logs in to the real Deel with details you type in (not echoed or saved)
and reads every endpoint once, printing a summary without amounts or names.
