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

    digits = extract_digits(raw)

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
