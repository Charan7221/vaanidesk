"""Phone number normalisation (E.164), so SMS confirmations reach Indian mobiles."""
from __future__ import annotations

import re


def normalize_phone(raw: str | None, default_country_code: str = "+91") -> str | None:
    """'9346968532' -> '+919346968532'; '09346968532' -> '+919346968532'; keeps '+1...' as is."""
    if not raw:
        return None
    s = raw.strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if plus:
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        return default_country_code + digits
    if len(digits) == 12 and digits.startswith(default_country_code.lstrip("+")):
        return "+" + digits
    # No "+" given: treat as a local number (so "12345678910" is an 11-digit Indian number -> invalid,
    # not silently a US number).
    return default_country_code + digits


def validate_phone(raw: str | None, default_country_code: str = "+91") -> tuple[str | None, str | None]:
    """Return (E.164 number, None) if plausible, else (None, reason).

    Indian mobiles: +91 followed by 10 digits starting with 6, 7, 8 or 9.
    Other countries: 8-15 digits in total (E.164 limit).
    """
    number = normalize_phone(raw, default_country_code)
    if not number:
        return None, "no digits in the phone number"
    digits = number[1:]
    if number.startswith("+91"):
        local = digits[2:]
        if len(local) != 10:
            return None, f"Indian mobile numbers have 10 digits, but {len(local)} were given"
        if local[0] not in "6789":
            return None, "Indian mobile numbers start with 6, 7, 8 or 9"
        if len(set(local)) <= 2:
            return None, "the number looks like a placeholder"
        return number, None
    if not 8 <= len(digits) <= 15:
        return None, "phone numbers have 8 to 15 digits including the country code"
    return number, None
