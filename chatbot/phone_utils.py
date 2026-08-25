import logging
import re
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

CHILE_COUNTRY_CODE = "56"
CHILE_MOBILE_PREFIXES = {"9", "1"}  # 9 for mobile, 1 for some VOIP
CHILE_LANDLINE_PREFIXES = {"2", "3", "4", "5", "6", "7"}

MIN_VALID_PHONE_DIGITS = 8
MAX_VALID_PHONE_DIGITS = 15


def extract_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _collapse_repeated_chile_country_code(digits: str) -> str:
    """Remove a duplicated ``56`` prefix found in some portal payloads.

    A Chilean mobile is ``56 9 XXXXXXXX``.  A few sources have delivered the
    same value as ``56 56 9 XXXXXXXX`` (for example ``+5656959401891``).
    Keeping that value as-is makes the same person look like a different lead.
    The correction is deliberately narrow: only a 13-digit value with the
    duplicated prefix and a valid Chilean mobile remainder is changed.
    """
    if len(digits) == 13 and digits.startswith("5656") and digits[4] == "9":
        candidate = digits[2:]
        if len(candidate) == 11 and candidate.startswith("569"):
            return candidate
    return digits


def phone_identity_variants(value: str) -> list[str]:
    """Return canonical and known legacy spellings for identity lookups."""
    digits = extract_digits(str(value or ""))
    normalized = normalize_phone_strict(value) if digits else None
    if normalized:
        canonical_digits = extract_digits(normalized)
    else:
        canonical_digits = _collapse_repeated_chile_country_code(digits)

    variants = []
    legacy_repeated_prefix = (
        "56" + canonical_digits
        if canonical_digits.startswith("569") and len(canonical_digits) == 11
        else ""
    )
    for candidate in (canonical_digits, legacy_repeated_prefix, digits):
        if candidate and candidate not in variants:
            variants.append(candidate)
            variants.append("+" + candidate)
    return variants


def is_chilean_number(digits: str) -> bool:
    if not digits:
        return False
    if digits.startswith(CHILE_COUNTRY_CODE):
        return True
    if len(digits) == 9 and digits[0] == "9":
        return True
    if len(digits) == 8:
        return True
    return False


def normalize_phone(raw: str) -> Tuple[Optional[str], str, bool]:
    raw = (raw or "").strip()
    if not raw:
        return None, "EMPTY", False

    digits = _collapse_repeated_chile_country_code(extract_digits(raw))

    if len(digits) < MIN_VALID_PHONE_DIGITS:
        return None, "TOO_SHORT", False

    if len(digits) > MAX_VALID_PHONE_DIGITS:
        return None, "TOO_LONG", False

    if is_chilean_number(digits):
        if digits.startswith(CHILE_COUNTRY_CODE):
            normalized = "+" + digits
        elif len(digits) == 9 and digits[0] == "9":
            normalized = "+56" + digits
        elif len(digits) == 8 and digits[0] == "2":
            normalized = "+562" + digits
        elif len(digits) == 8:
            normalized = "+56" + digits
        else:
            normalized = "+56" + digits
        if len(normalized.strip("+")) < 9:
            return None, "INVALID_CHILEAN", False
        return normalized, "CHILEAN_VALID", True

    if digits.startswith("+"):
        normalized = "+" + digits
    elif digits.startswith("00"):
        normalized = "+" + digits[2:]
    elif len(digits) >= 10:
        normalized = "+" + digits
    else:
        return None, "UNKNOWN_FORMAT", False

    if len(normalized.strip("+")) < 8:
        return None, "INVALID_INTERNATIONAL", False

    return normalized, "INTERNATIONAL_VALID", True


def normalize_phone_strict(raw: str) -> Optional[str]:
    phone, status, valid = normalize_phone(raw)
    if valid:
        return phone
    return None


SYNTHETIC_PHONE_PREFIX = "no-phone-"


def is_synthetic_phone(phone: Optional[str]) -> bool:
    """Detecta si un valor de phone es sintético (no real)."""
    if not phone:
        return True
    return phone.startswith(SYNTHETIC_PHONE_PREFIX) or phone == "+56900000000"


def build_synthetic_phone_key(source_system: str, source_event_id: str) -> str:
    """Genera una clave técnica determinística para contactos sin teléfono.
    Formato: no-phone-<source_system>-<source_event_id>
    Garantiza: mismo evento → misma clave, sin colisiones entre canales.
    """
    system = re.sub(r"[^a-z0-9]", "", source_system.lower().strip())
    event = re.sub(r"[^a-z0-9]", "", str(source_event_id).lower().strip())
    key = f"{SYNTHETIC_PHONE_PREFIX}{system}-{event}"
    if len(key) > 80:
        key = key[:80]
    return key
