"""Explainable owner-prioritization score independent from classifier confidence."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

OWNER_SCORE_VERSION = "owner-score-signals-v1"


def _text(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", value.lower()).strip()


def _first(doc: dict, *keys: str):
    details = doc.get("details") or {}
    snapshot = doc.get("source_signal_snapshot") or {}
    for key in keys:
        value = doc.get(key)
        if value in (None, "", [], {}):
            value = details.get(key)
        if value in (None, "", [], {}):
            value = snapshot.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def calculate_owner_score(doc: dict, calculated_at: datetime | None = None) -> dict:
    """Calculate a score from observable signals; state/confidence never affect it."""
    positive: list[dict] = []
    negative: list[dict] = []
    sources: list[str] = []

    def add(bucket: list[dict], code: str, weight: int, evidence: str, source: str):
        if any(item["code"] == code for item in bucket):
            return
        bucket.append({"code": code, "weight": weight, "evidence": evidence[:240], "source": source})
        sources.append(source)

    seller_type_raw = _first(doc, "seller_type")
    seller_type = _text(seller_type_raw)
    if seller_type in {"particular", "persona", "private"}:
        add(positive, "seller_type_particular", 5, str(seller_type_raw), "seller_type")
    elif any(word in seller_type for word in ("empresa", "profesional", "corredor", "agente", "inmobiliaria")):
        add(negative, "seller_type_commercial", -35, str(seller_type_raw), "seller_type")

    if bool(_first(doc, "seller_is_pro")):
        add(negative, "professional_badge", -35, "Perfil marcado como profesional", "seller_is_pro")

    brand_values = []
    for key in ("company_name", "broker_brand"):
        value = _first(doc, key)
        if value and _text(value) not in {"n/a", "si", "no", "particular"}:
            brand_values.append(f"{key}={value}")
    if brand_values:
        add(negative, "commercial_brand", -35, "; ".join(brand_values), "company_name/broker_brand")

    identity_raw = _first(doc, "publicador_visible", "seller_name", "contact_name") or ""
    identity = _text(identity_raw)
    commercial_identity = re.search(r"\b(propiedades|inmobiliaria|corredor(?:a)?|real estate|gest(?:ion|ora)|spa|limitada|ltda)\b", identity)
    if commercial_identity:
        add(negative, "commercial_publisher_identity", -30, str(identity_raw), "publicador_visible")
    elif identity and identity not in {"particular", "usuario", "dueno", "duena"}:
        words = re.findall(r"[a-z]{2,}", identity)
        if 2 <= len(words) <= 4 and not any(char.isdigit() for char in identity):
            add(positive, "personal_publisher_identity", 5, str(identity_raw), "publicador_visible")

    description_raw = _first(doc, "description", "descripcion") or ""
    description = _text(description_raw)
    explicit_patterns = (
        r"\bsoy (?:el |la )?(?:dueno|duena|propietario|propietaria)\b",
        r"\bvendo mi (?:casa|departamento|propiedad|parcela|terreno)\b",
        r"\barriendo mi (?:casa|departamento|propiedad|parcela|terreno)\b",
    )
    explicit = next((m.group(0) for pattern in explicit_patterns if (m := re.search(pattern, description))), None)
    if explicit:
        add(positive, "first_person_owner_statement", 35, explicit, "description")

    commercial_terms = sorted(set(re.findall(
        r"\b(?:corredora|corretaje|inmobiliaria|comision|honorarios|asesor(?:a)? inmobiliari[oa])\b", description
    )))
    if commercial_terms:
        add(negative, "commercial_description", -20, ", ".join(commercial_terms), "description")

    url = _text(doc.get("url"))
    if "/profesional/" in url or "publicador-profesional" in url:
        add(negative, "professional_url", -35, str(doc.get("url")), "url")

    activity = _first(doc, "publisher_activity")
    if isinstance(activity, dict):
        distinct = activity.get("distinct_listings_in_window")
        days = activity.get("window_days")
        try:
            distinct_i, days_i = int(distinct), int(days)
        except (TypeError, ValueError):
            distinct_i = days_i = 0
        if days_i and days_i <= 90 and distinct_i >= 8 and negative:
            add(negative, "high_distinct_listing_activity", -10,
                f"{distinct_i} inmuebles distintos en {days_i} días", "publisher_activity")

    score = max(0, min(100, 50 + sum(x["weight"] for x in positive + negative)))
    when = calculated_at or datetime.now(timezone.utc)
    return {
        "owner_score": score,
        "owner_score_signals": {
            "base_score": 50,
            "positive": positive,
            "negative": negative,
            "neutral": not positive and not negative,
            "signal_sources": sorted(set(sources)),
        },
        "owner_score_version": OWNER_SCORE_VERSION,
        "owner_score_calculated_at": when,
    }
