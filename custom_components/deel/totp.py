"""RFC 6238 one-time codes, so Home Assistant can answer Deel's authenticator-app 2FA."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
from urllib.parse import parse_qs, urlparse

PERIOD = 30
DIGITS = 6
MIN_SECRET_BYTES = 10  # 80 bits; authenticator apps use 80-160


def normalize_secret(raw: str) -> str | None:
    """Accept a base32 key (any case, spaces/dashes allowed) or an otpauth:// URI.

    Returns the canonical upper-case key without padding, or None if it is not a usable secret.
    """
    text = raw.strip()
    if text.lower().startswith("otpauth://"):
        values = parse_qs(urlparse(text).query).get("secret")
        if not values:
            return None
        text = values[0]
    key = re.sub(r"[\s-]", "", text).upper().rstrip("=")
    if not key or not re.fullmatch(r"[A-Z2-7]+", key):
        return None
    try:
        decoded = _decode(key)
    except binascii.Error:
        return None
    return key if len(decoded) >= MIN_SECRET_BYTES else None


def _decode(key: str) -> bytes:
    return base64.b32decode(key + "=" * (-len(key) % 8))


def code_at(secret: str, timestamp: float, digits: int = DIGITS, period: int = PERIOD) -> str:
    """The code an authenticator app shows at ``timestamp`` (Unix seconds)."""
    counter = int(timestamp // period)
    digest = hmac.new(_decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def seconds_left(timestamp: float, period: int = PERIOD) -> float:
    """How long the code valid at ``timestamp`` stays valid."""
    return period - (timestamp % period)
