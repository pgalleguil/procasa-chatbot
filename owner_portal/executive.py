"""Read-only, identity-bound executive contact presentation."""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import quote, urlparse


def resolve_executive_contact(db: Any, row: Mapping, monthly: Mapping, name: str) -> dict:
    snapshot = row.get("campaign_snapshot") or {}
    saved = snapshot.get("executive") if isinstance(snapshot.get("executive"), Mapping) else {}
    current = monthly.get("executive") if isinstance(monthly.get("executive"), Mapping) else {}
    if saved.get("name") and str(saved["name"]).strip().casefold() != name.strip().casefold():
        saved = {}
    if current.get("name") and str(current["name"]).strip().casefold() != name.strip().casefold():
        current = {}
    email = str(saved.get("email") or snapshot.get("executive_email") or row.get("executive_email") or current.get("email") or "").strip().casefold()
    if current.get("email") and email and str(current["email"]).strip().casefold() != email:
        current = {}
    # No partial-name/fuzzy matches: a directory contact must have one exact identity.
    query = {"email": re.compile(r"^" + re.escape(email) + r"$", re.I)} if email else {"nombre": re.compile(r"^" + re.escape(name.strip()) + r"$", re.I)}
    directory = list(db["usuarios"].find({**query, "is_active": {"$ne": False}}, {
        "nombre": 1, "email": 1, "phone": 1, "telefono": 1, "tel": 1, "movil": 1,
        "cargo": 1,
        "photo_url": 1, "foto_url": 1, "avatar_url": 1, "photo_verified": 1,
    }).limit(2)) if (email or name and name not in {"Ejecutivo PROCASA", "Tu ejecutivo PROCASA", "Equipo PROCASA"}) else []
    user = directory[0] if len(directory) == 1 else {}
    # A monthly contact carrying a different identity must not replace the campaign executive.
    phone = str(current.get("phone") or saved.get("phone") or snapshot.get("executive_phone") or row.get("executive_phone") or user.get("phone") or user.get("telefono") or user.get("tel") or user.get("movil") or "").strip()
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 9 and digits.startswith("9"):
        digits = "56" + digits
    if not re.fullmatch(r"569\d{8}", digits):
        digits = ""
    display_phone = f"+56 9 {digits[3:7]} {digits[7:]}" if digits else ""
    photo = str(user.get("photo_url") or user.get("foto_url") or user.get("avatar_url") or "").strip() if user.get("photo_verified") is True else ""
    for contact in (saved, current):
        if contact.get("photo_verified") is True and contact.get("photo_url"):
            photo = str(contact["photo_url"]).strip()
    parsed = urlparse(photo)
    if not (photo.startswith("/static/") or parsed.scheme == "https" and parsed.netloc and not parsed.username):
        photo = ""
    resolved_name = name or str(user.get("nombre") or "Ejecutivo PROCASA")
    initials = "".join(part[0] for part in resolved_name.split()[:2]).upper() or "PC"
    email = email or str(user.get("email") or "").strip().casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        email = ""
    return {"name": resolved_name, "role": "Agente inmobiliario · PROCASA SUCRE",
            "email": email, "phone": display_phone, "phone_digits": digits, "photo_url": photo, "initials": initials}


def whatsapp_destination(contact: Mapping, property_code: str) -> str:
    digits = str(contact.get("phone_digits") or "")
    if not re.fullmatch(r"569\d{8}", digits):
        return ""
    executive_name = str(contact.get("name") or "").strip()
    executive_first = executive_name.split()[0] if executive_name else "equipo PROCASA"
    property_reference = f" la propiedad código {property_code}" if str(property_code or "").strip() else " mi propiedad"
    message = (
        f"Hola {executive_first}, te contacto por el informe comercial de{property_reference}. "
        "Quisiera conversar contigo sobre la recomendación de precio y las alternativas disponibles."
    )
    return f"https://wa.me/{digits}?text={quote(message)}"
