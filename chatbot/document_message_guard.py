"""Guarda temporal del chatbot para procesos de firma enviados por WhatsApp."""

from datetime import datetime, timezone
import re
from typing import Any, Dict, Optional


ACTIVE_DOCUMENT_STATUSES = (
    "sent",
    "opened",
    "viewed",  # Compatibilidad con documentos antiguos.
    "otp_requested",
    "otp_verified",
    "signed",
    "accepted",
)


def normalize_phone_digits(phone: Any) -> str:
    """Retorna solo los dígitos y agrega el código de Chile cuando corresponde."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 9 and digits.startswith("9"):
        return "56" + digits
    return digits


def build_phone_suffix_regex(phone: Any) -> Optional[re.Pattern]:
    """Construye una búsqueda tolerante a +, espacios y guiones en teléfonos guardados."""
    digits = normalize_phone_digits(phone)
    if len(digits) < 8:
        return None
    # Los últimos 9 dígitos identifican el móvil chileno sin depender de +56.
    suffix = digits[-9:]
    return re.compile(r"\D*".join(re.escape(ch) for ch in suffix) + r"\D*$")


def find_active_document_guard(db: Any, phone: Any, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Busca un convenio u orden vigente que deba silenciar el chatbot.

    La vigencia proviene del mismo ``security.token_expiry`` usado por la firma,
    por lo que un reenvío renueva automáticamente el bloqueo por otras 120 horas.
    """
    phone_pattern = build_phone_suffix_regex(phone)
    if phone_pattern is None:
        return None

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    query = {
        "phone": phone_pattern,
        "status": {"$in": list(ACTIVE_DOCUMENT_STATUSES)},
        "security.token_expiry": {"$gt": now},
    }
    projection = {
        "contract_code": 1,
        "visita_code": 1,
        "status": 1,
        "security.token_expiry": 1,
    }

    for collection_name, document_type in (
        ("contracts", "contract"),
        ("visitas", "visita"),
    ):
        document = db[collection_name].find_one(query, projection)
        if document:
            return {
                "document_type": document_type,
                "document_code": document.get("contract_code") or document.get("visita_code"),
                "status": document.get("status"),
                "expires_at": (document.get("security") or {}).get("token_expiry"),
            }
    return None
