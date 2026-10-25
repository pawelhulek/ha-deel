"""TOTP codes and secret parsing."""

from __future__ import annotations

import pytest

from custom_components.deel.totp import code_at, normalize_secret, seconds_left
from tests.fake_deel import TOTP_SECRET


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        # RFC 6238 appendix B, SHA-1, 8 digits.
        (59, "94287082"),
        (1111111109, "07081804"),
        (1234567890, "89005924"),
        (20000000000, "65353130"),
    ],
)
def test_rfc6238_vectors(timestamp: int, expected: str) -> None:
    assert code_at(TOTP_SECRET, timestamp, digits=8) == expected


def test_six_digit_code_is_the_tail_of_the_eight_digit_one() -> None:
    assert code_at(TOTP_SECRET, 59) == "287082"


def test_seconds_left() -> None:
    assert seconds_left(60) == 30
    assert seconds_left(89.5) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "raw",
    [
        TOTP_SECRET,
        TOTP_SECRET.lower(),
        "gezd gnbv gy3t qojq gezd gnbv gy3t qojq",
        "GEZD-GNBV-GY3T-QOJQ-GEZD-GNBV-GY3T-QOJQ",
        f"{TOTP_SECRET}====",
        f"otpauth://totp/Deel:worker@example.com?secret={TOTP_SECRET}&issuer=Deel",
    ],
)
def test_normalize_accepts_common_formats(raw: str) -> None:
    assert normalize_secret(raw) == TOTP_SECRET


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "123456",  # a code, not a secret
        "GEZDGNBV",  # too short
        "NOT*BASE32!",
        "otpauth://totp/Deel?issuer=Deel",
        "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJ1",  # '1' is not base32
    ],
)
def test_normalize_rejects_non_secrets(raw: str) -> None:
    assert normalize_secret(raw) is None
