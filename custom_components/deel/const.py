"""Constants for the Deel integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "deel"
MANUFACTURER: Final = "Deel"
BASE_URL: Final = "https://app.deel.com"
APP_HOST: Final = "app.deel.com"
SCAN_INTERVAL: Final = timedelta(hours=1)
# Log in again this long before the 24-hour token runs out.
TOKEN_REFRESH_MARGIN: Final = timedelta(minutes=15)

CONF_TOTP_SECRET: Final = "totp_secret"
CONF_TOKEN: Final = "token"
CONF_TOKEN_EXPIRES: Final = "token_expires"
