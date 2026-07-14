"""Explainable owner-priority score independent from classification confidence."""
from __future__ import annotations

import re
import unicodedata
import hashlib
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return re.sub(r"\s+", " ", text.encode("ascii", "ignore").decode().lower()).strip()


OWNER_PATTERNS = {
    "owner_explicit": re.compile(
        r"\bsoy (?:el |la )?(?:dueno|duena|propietario|propietaria)\b|"
        r"\bsomos (?:los |las )?(?:duenos|duenas|dueos|propietarios|propietarias)\b"
    ),
    "first_person_property": re.compile(
        r"\b(?:vendo|arriendo|alquilo) (?:mi|mis|nuestra|nuestro)\s+"
        r"(?:casa|departamento|depto|propiedad|parcela|terreno)\b"
    ),
}

COMMERCIAL_TERMS = (
    "corredor", "corredora", "corretaje", "inmobiliaria", "propiedades",
    "real estate", "gestion inmobiliaria", "broker", "remax", "re/max",
    "century 21", "engel", "grecop",
)
BROKER_DESCRIPTION_TERMS = (
    "comision de corretaje", "comision por corretaje", "honorarios de corretaje",
    "gestion de arriendo",
)
NON_PERSONAL_IDENTITY_TERMS = (
    "spa", "ltda", "eirl", "propiedades", "inmobiliaria", "corredor",
    "servicios", "grupo", "turismo", "alimento", "constructora", "chile",
)
GENERIC_PUBLISHER_IDENTITIES = {
    "particular", "propietario", "propietaria", "agente", "profesional",
    "empresa", "inmobiliaria", "corredor", "corredora", "usuario",
}
PERSON_NAME_RE = re.compile(
    r"^[a-z]+(?:\s+[a-z]+){1,3}$"
)


@dataclass(frozen=True)
class OwnerScoreResult:
    score: int
    base_score: int
    signals: tuple[dict[str, Any], ...]
    useful_signal_count: int
    version: str = "owner-score-v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "base_score": self.base_score,
            "signals": list(self.signals),
            "useful_signal_count": self.useful_signal_count,
            "version": self.version,
        }


def calculate_owner_score(data: dict[str, Any]) -> OwnerScoreResult:
    """Return a 0-100 ranking score with an auditable signal breakdown.

    This is deliberately not a calibrated probability. The neutral fallback is
    50 and is retained only when no useful positive or negative signal exists.
    """
    description = _norm(data.get("description") or data.get("descripcion"))
    identity_parts = [
        data.get("publicador"), data.get("publicador_visible"),
        data.get("company_name"), data.get("broker_brand"),
        data.get("listing_advertiser"), data.get("contact_logo_alt"),
    ]
    identity = _norm(" ".join(str(x or "") for x in identity_parts))
    seller_type = _norm(data.get("seller_type"))
    publicador = _norm(data.get("publicador") or data.get("publicador_visible"))
    signals: list[dict[str, Any]] = []

    def add(code: str, weight: int, evidence: str) -> None:
        signals.append({"code": code, "weight": weight, "evidence": evidence[:240]})

    if OWNER_PATTERNS["owner_explicit"].search(description):
        add("OWNER_EXPLICIT", 35, "La descripción declara dueño/propietario directo")
    if OWNER_PATTERNS["first_person_property"].search(description):
        add("FIRST_PERSON_OWNERSHIP", 30, "Primera persona posesiva: vendo/arriendo mi propiedad")

    commercial_hits = sorted({term for term in COMMERCIAL_TERMS if term in identity})
    if commercial_hits:
        add("COMMERCIAL_IDENTITY", -45, ", ".join(commercial_hits))
    if bool(data.get("seller_is_pro")):
        add("PROFESSIONAL_BADGE", -35, "El portal identifica al publicador como profesional")
    if seller_type in {"agente", "profesional", "empresa", "inmobiliaria", "corredor"}:
        add("PROFESSIONAL_SELLER_TYPE", -20, f"seller_type={seller_type}")

    broker_desc_hits = sorted({term for term in BROKER_DESCRIPTION_TERMS if term in description})
    if broker_desc_hits:
        add("BROKER_DESCRIPTION", -50, ", ".join(broker_desc_hits))

    activity = data.get("publisher_activity") or {}
    try:
        unique_properties = int(activity.get("unique_properties") or 0)
        window_days = int(activity.get("window_days") or 0)
        reposts_same_property = int(activity.get("reposts_same_property") or 0)
    except (AttributeError, TypeError, ValueError):
        unique_properties = window_days = reposts_same_property = 0
    if 1 <= window_days <= 90 and unique_properties >= 8:
        add(
            "MULTI_PUBLISHER_HIGH_90D", -30,
            f"{unique_properties} inmuebles distintos en {window_days} días; "
            f"{reposts_same_property} republicaciones del mismo inmueble",
        )
    elif 1 <= window_days <= 90 and unique_properties >= 4:
        add(
            "MULTI_PUBLISHER_90D", -15,
            f"{unique_properties} inmuebles distintos en {window_days} días; "
            f"{reposts_same_property} republicaciones del mismo inmueble",
        )

    # A human-looking name is weak evidence and is ignored when the same
    # record already exposes a commercial identity.
    looks_personal = (
        publicador and PERSON_NAME_RE.fullmatch(publicador)
        and not any(term in publicador for term in NON_PERSONAL_IDENTITY_TERMS)
    )
    if looks_personal and not commercial_hits:
        add("PERSONAL_IDENTITY", 7, publicador)
    if seller_type == "particular":
        add("PARTICULAR_BADGE", 5, "seller_type=particular")
    elif seller_type == "propietario" and not commercial_hits:
        add("OWNER_TYPE_BADGE", 8, "seller_type=propietario")

    score = max(0, min(100, 50 + sum(int(s["weight"]) for s in signals)))
    return OwnerScoreResult(
        score=score,
        base_score=50,
        signals=tuple(signals),
        useful_signal_count=len(signals),
    )


def propose_classification_state(result: OwnerScoreResult) -> str:
    """Derive state from auditable signals, never from the numeric score alone."""
    codes = {signal["code"] for signal in result.signals}
    if codes & {"COMMERCIAL_IDENTITY", "PROFESSIONAL_BADGE", "BROKER_DESCRIPTION"}:
        return "CORREDOR_SEGURO"
    if codes & {"OWNER_EXPLICIT", "FIRST_PERSON_OWNERSHIP"} and not any(
        signal["weight"] < 0 for signal in result.signals
    ):
        return "DUE\u00d1O_SEGURO"
        return "DUEÑO_SEGURO"
    return "INCIERTO"


def build_source_signal_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Persist original classifier inputs without inferring missing values."""
    activity = data.get("publisher_activity") or {}
    return {
        "company_name": data.get("company_name") or "",
        "broker_brand": data.get("broker_brand") or "",
        "seller_type": data.get("seller_type") or "",
        "seller_is_pro": bool(data.get("seller_is_pro")),
        "publisher_visible": (
            data.get("publicador") or data.get("publicador_visible")
            or data.get("seller_name") or data.get("contact_name") or ""
        ),
        "publisher_profile_id": data.get("seller_profile_id") or "",
        "publisher_activity": {
            "window_days": activity.get("window_days"),
            "unique_properties": activity.get("unique_properties"),
            "reposts_same_property": activity.get("reposts_same_property"),
            "total_publications": activity.get("total_publications"),
            "source": activity.get("source") or "",
        },
        "classifier_original_signals": data.get("classifier_original_signals") or {},
    }


def publisher_identity_key(data: dict[str, Any]) -> str:
    profile_id = str(data.get("seller_profile_id") or "").strip()
    if profile_id and profile_id not in {"N/A", "S/I"}:
        return f"profile:{profile_id}"
    identity = _norm(
        data.get("company_name") or data.get("broker_brand")
        or data.get("publicador") or data.get("publicador_visible")
    )
    if identity in GENERIC_PUBLISHER_IDENTITIES:
        return ""
    return f"identity:{identity}" if identity else ""


def property_fingerprint(data: dict[str, Any]) -> str:
    explicit = str(data.get("property_fingerprint") or data.get("canonical_property_id") or "").strip()
    if explicit:
        return explicit
    parts = [
        _norm(data.get("comuna")), _norm(data.get("title") or data.get("titulo")),
        _norm(data.get("price") or data.get("precio_raw")),
        _norm(data.get("direccion") or data.get("address")),
    ]
    stable = "|".join(parts)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest() if any(parts) else ""


def compute_publisher_activity(
    current: dict[str, Any], historical: list[dict[str, Any]],
    *, window_days: int = 90, now: datetime | None = None,
) -> dict[str, Any]:
    """Count distinct properties and reposts for one identity in a time window."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    identity_key = publisher_identity_key(current)
    fingerprints: list[str] = []
    total = 0
    for item in [*historical, current]:
        if not identity_key or publisher_identity_key(item) != identity_key:
            continue
        raw_date = item.get("published_at") or item.get("fecha_publicacion") or item.get("processed_at")
        try:
            event_date = raw_date if isinstance(raw_date, datetime) else datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            if event_date.tzinfo is None:
                event_date = event_date.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            # Missing dates cannot support a temporal multipublisher penalty.
            continue
        if event_date < cutoff or event_date > now + timedelta(days=1):
            continue
        fingerprint = property_fingerprint(item)
        if not fingerprint:
            fingerprint = str(item.get("listing_id") or item.get("url") or "").strip()
        if not fingerprint:
            continue
        total += 1
        fingerprints.append(fingerprint)
    unique = len(set(fingerprints))
    return {
        "window_days": window_days,
        "unique_properties": unique,
        "reposts_same_property": max(0, total - unique),
        "total_publications": total,
        "source": "publisher_history_distinct_fingerprint_v1",
        "identity_key": identity_key,
    }
