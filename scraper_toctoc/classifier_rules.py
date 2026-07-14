from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from typing import Any, Iterable

from config import DATA_DIR

STRONG_PUBLISHER_FIELDS = (
    "publicador_visible", "contact_name", "contact_logo_alt",
    "seller_jsonld_name", "listing_advertiser", "contact_badges_text",
)
WEAK_CONTEXT_FIELDS = (
    "title", "description", "descripcion", "seller_text", "body_text", "seller_type",
)


@lru_cache(maxsize=1)
def load_rule_sets() -> dict[str, Any]:
    rule_files = {
        "known_broker_brands": ("known_broker_brands.json", "brands"),
        "hard_broker_terms": ("hard_broker_terms.json", "hard_broker_terms"),
        "company_shape_terms": ("company_shape_terms.json", "company_shape_terms"),
        "listing_removed_patterns": ("listing_removed_patterns.json", "patterns"),
        "search_terms": ("search_terms.json", "search_terms"),
        "owner_keywords": ("owner_keywords.json", "owner_terms"),
    }
    sets: dict[str, Any] = {}
    for name, (filename, key) in rule_files.items():
        path = DATA_DIR / filename
        if not path.exists(): sets[name] = []; continue
        try: data = json.loads(path.read_text(encoding="utf-8"))
        except Exception: sets[name] = []; continue
        if isinstance(data, dict): values = data.get(key, [])
        elif isinstance(data, list): values = data
        else: values = []
        if not isinstance(values, list): values = []
        sets[name] = [str(item) for item in values if str(item).strip()]
    return sets


@lru_cache(maxsize=4096)
def normalize_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.replace("/", " ").replace("-", " ").replace("_", " ")
    raw = re.sub(r"[^a-z0-9+ ]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


@lru_cache(maxsize=4096)
def _matchable(text: str) -> str:
    return re.sub(r"\s+", "", normalize_text(text))


def _field_values(extracted: dict[str, Any], fields: Iterable[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in fields:
        value = extracted.get(field, "")
        if value is None: value = ""
        if isinstance(value, (list, dict)): value = " ".join(str(i) for i in (value if isinstance(value, list) else value.items()))
        values[field] = str(value)
    return values


def _all_text(extracted: dict[str, Any]) -> str:
    return normalize_text(" ".join(_field_values(extracted, STRONG_PUBLISHER_FIELDS + WEAK_CONTEXT_FIELDS).values()))


def _search_terms(text: str, terms: Iterable[str]) -> list[str]:
    return [t for t in terms if normalize_text(t) and normalize_text(t) in text]


def _evidence_from_fields(extracted: dict[str, Any], fields: Iterable[str], terms: Iterable[str]) -> list[str]:
    evidence: list[str] = []
    for field, value in _field_values(extracted, fields).items():
        m = _matchable(value)
        for term in terms:
            if _matchable(term) and _matchable(term) in m:
                evidence.append(f"{field}:{term}")
    return evidence


def is_removed_listing(extracted: dict[str, Any]) -> dict[str, Any] | None:
    status = str(extracted.get("html_validation_status", "")).upper()
    if status == "LISTING_REMOVED":
        return {"state": "AD_REMOVED", "confidence": 1.0, "reason": "HTML validation detected removed listing.", "evidence": [str(extracted.get("html_validation_reason", "listing_removed"))], "source": "html_validation"}
    return None


def known_broker_brand_in_strong_fields(extracted: dict[str, Any]) -> dict[str, Any] | None:
    evidence = _evidence_from_fields(extracted, STRONG_PUBLISHER_FIELDS, load_rule_sets()["known_broker_brands"])
    if not evidence: return None
    return {"state": "CORREDOR_SEGURO", "confidence": 0.99, "reason": "Known broker brand found in strong publisher fields.", "evidence": evidence[:8], "source": "rules_json"}


def hard_broker_terms_in_strong_fields(extracted: dict[str, Any]) -> dict[str, Any] | None:
    evidence = _evidence_from_fields(extracted, STRONG_PUBLISHER_FIELDS, load_rule_sets()["hard_broker_terms"])
    if not evidence: return None
    return {"state": "CORREDOR_PROBABLE" if len(evidence) == 1 else "CORREDOR_SEGURO", "confidence": 0.9 if len(evidence) == 1 else 0.95, "reason": "Hard broker terms found.", "evidence": evidence[:8], "source": "rules_json"}


def detect_owner_signals(extracted: dict[str, Any]) -> list[str]:
    return _search_terms(_all_text(extracted), load_rule_sets()["owner_keywords"])


def company_shape_in_publisher_fields(extracted: dict[str, Any]) -> dict[str, Any]:
    evidence = _evidence_from_fields(extracted, STRONG_PUBLISHER_FIELDS, load_rule_sets()["company_shape_terms"])
    return {"company_like_suspected": bool(evidence), "company_like_evidence": evidence[:8]}


def _all_evidence_context(extracted: dict[str, Any]) -> dict[str, Any]:
    brand = known_broker_brand_in_strong_fields(extracted)
    hard = hard_broker_terms_in_strong_fields(extracted)
    company = company_shape_in_publisher_fields(extracted)
    owner = detect_owner_signals(extracted)
    removed = is_removed_listing(extracted)
    return {"removed": removed, "known_brand_evidence": brand["evidence"] if brand else [], "hard_broker_evidence": hard["evidence"] if hard else [], "owner_signal_evidence": owner, "weak_broker_evidence": [], **company}


def classify_structural_broker(extracted: dict[str, Any]) -> dict[str, Any] | None:
    signals = {}
    evidence: list[str] = []

    seller_type = str(extracted.get("seller_type", "")).upper()
    st_source = str(extracted.get("seller_type_source", ""))
    st_evidence = str(extracted.get("seller_type_evidence", ""))
    url_format = str(extracted.get("url_format", ""))
    seller_name = str(extracted.get("publicador_visible", "") or extracted.get("seller_name", "") or "")
    listing_advertiser = str(extracted.get("listing_advertiser", "") or "")
    text_fields = " ".join(str(extracted.get(k, "")) for k in
                           ("title", "description", "descripcion", "seller_text", "seller_name",
                            "publicador_visible", "contact_name", "body_text"))

    # Structural signals from seller type / URL
    if seller_type == "EMPRESA" and "inmobiliaria" in st_source.lower():
        signals["empresa_explicita"] = True
        signals["inmobiliaria_por_url"] = True
        evidence.append(f"seller_type={seller_type}")
        evidence.append(f"seller_type_source={st_source}")

    if seller_type == "EMPRESA" and ("inmobiliarias" in st_evidence.lower() or "corredoras" in st_evidence.lower()):
        signals["perfil_inmobiliaria"] = True
        evidence.append(f"publisher profile is under /inmobiliarias/")

    if url_format == "compranuevo" and seller_name:
        signals["compranuevo"] = True
        evidence.append(f"url_format={url_format}")

    if "inmobiliaria" in seller_name.lower() or "inmobiliaria" in listing_advertiser.lower():
        signals["nombre_inmobiliaria"] = True
        evidence.append(f"seller_name contains 'inmobiliaria'")

    if "corredora" in seller_name.lower() or "corredores" in seller_name.lower():
        signals["nombre_corredora"] = True
        evidence.append(f"seller_name contains 'corredora'")

    # Professional project signals in text fields
    proj_signals = detect_professional_project_signals(text_fields)
    has_strong = False
    has_weak = False
    if proj_signals:
        signals["proyecto_profesional"] = True
        for ps in proj_signals:
            evidence.append(f"project_signal:{ps['signal']} evidence={ps['evidence']}")
            if ps.get('level') == 'strong':
                has_strong = True
            elif ps.get('level') == 'weak':
                has_weak = True

    if signals and evidence:
        is_structural = any(s in signals for s in ("empresa_explicita", "compranuevo", "nombre_inmobiliaria", "nombre_corredora", "perfil_inmobiliaria"))
        has_project = signals.get("proyecto_profesional", False)
        
        # Strong professional signals (corretaje, comision, etc.) -> CORREDOR_SEGURO
        if has_strong or is_structural:
            return {
                "state": "CORREDOR_SEGURO",
                "confidence": 1.0 if is_structural else 0.95,
                "reason": "Evidencia profesional explicita: " + (evidence[-1][:100] if evidence else "senales de corretaje"),
                "evidence": evidence[:10],
                "source": "structural_rules",
                "decision_pattern": "explicit_inmobiliaria" if is_structural else "strong_professional_signal",
                "signals": signals,
                "strong_signal_found": has_strong,
            }
        
        # Weak or project signals -> CORREDOR_PROBABLE
        if has_project:
            level = "weak_professional" if has_weak else "project_language"
            return {
                "state": "CORREDOR_PROBABLE",
                "confidence": 0.85 if has_weak else 0.7,
                "reason": "El contenido sugiere actividad profesional o de proyecto inmobiliario.",
                "evidence": evidence[:10],
                "source": "structural_rules",
                "decision_pattern": level,
                "signals": signals,
            }
    return None


EXPLICIT_OWNER_PHRASES = (
    "vende su dueno", "vende su dueño",
    "soy dueno", "soy el dueno", "soy dueño", "soy el dueño",
    "soy propietario", "soy propietaria",
    "vendo mi casa", "vendo mi departamento", "vendo mi propiedad",
    "arriendo mi casa", "arriendo mi departamento", "arriendo mi propiedad",
    "trato directo con dueno", "trato directo con dueño",
    "trato directo con propietario", "trato directo con la duena",
    "trato directo con la dueña", "trato directo con el dueno",
    "trato directo con el dueño",
    "dueno directo", "dueño directo",
    "propietario directo", "propietaria directa",
    "arriendo directo dueno", "arriendo directo dueño",
    "arrienda su dueno", "arrienda su dueño",
    "vende propietario", "propietario vende",
    "vende directamente su dueno", "vende directamente su dueño",
    "venta directa dueno", "venta directa dueño",
    # Shorter forms
    "arrienda dueno", "arrienda dueño",
    "dueno arrienda", "dueño arrienda",
    "arrienda directo dueno", "arrienda directo dueño",
    "arrienda su propia duena", "arrienda su propia dueña",
    "duena arrienda", "dueña arrienda",
    # Feminine forms
    "su duena vende", "vende su duena", "duena directa", "soy duena",
    "su dueña vende", "vende su dueña", "dueña directa", "soy dueña",
)

# Only first-person statements identify the publisher as the owner. The legacy
# tuple above remains for audit compatibility but is intentionally not used by
# the classifier because third-person/legal references describe the property,
# not the identity of whoever published the ad.
STRICT_EXPLICIT_OWNER_PHRASES = (
    "soy dueno", "soy el dueno", "soy la duena",
    "soy propietario", "soy propietaria",
    "somos los duenos", "somos las duenas",
    "somos los propietarios", "somos las propietarias",
    "vendo mi casa", "vendo mi departamento", "vendo mi depto",
    "vendo mi propiedad", "vendo mi parcela", "vendo mi terreno",
    "arriendo mi casa", "arriendo mi departamento", "arriendo mi depto",
    "arriendo mi propiedad", "alquilo mi casa", "alquilo mi departamento",
)

# Neutral contact phrases: do NOT prove ownership or professional status
NEUTRAL_CONTACT_PHRASES = frozenset({
    "contactame", "contáctame", "escribeme", "escríbeme",
    "llamame", "llámame",
    "agenda una visita", "agendar una visita", "coordinar visita",
    "coordina tu visita", "agenda tu visita",
    "disponible para visitas", "mas informacion por interno",
})

# Weak owner signals: suggestive but not conclusive alone
WEAK_OWNER_PHRASES = (
    "vende directamente",
    "venta directa",
    "arriendo directo",
    "sin comision",
    "sin comisión",
    "sin corredor",
    "sin corredora",
    "sin corredores",
    "no corredor",
    "no corredores",
    "se vende por",
    "propietario",
    "propietaria",
    "particular vende",
    "favor no llamar corredores",
)


def detect_weak_owner(extracted: dict[str, Any]) -> list[str]:
    """Detect weaker owner signals that need additional evidence."""
    text = normalize_text(" ".join(str(extracted.get(k, "")) for k in ("description", "descripcion", "title", "seller_text", "seller_name", "publicador_visible")))
    return [p for p in WEAK_OWNER_PHRASES if p in text]

# Strong professional signals: explicit evidence of real estate brokerage
STRONG_PROFESSIONAL_PATTERNS = {
    "comision_corretaje": r"\bcomisi[oó]n\s+corretaje\b",
    "honorarios_corretaje": r"\bhonorarios?\s+(de\s+)?corretaje\b",
    "corredora_propiedades": r"\bcorredora?\s+(de\s+)?propiedades\b",
    "corretaje": r"\bcorretaje\b",
    "comision_mas_iva": r"\bcomisi[oó]n\s+m[áa]s\s+iva\b",
    "vende_propiedades": r"\b(?:vende|arrienda)\s+(?:\w+\s+){1,6}(?:pro+p?i?e?dades|asesores?|inmobiliaria)\b",
    "vende_inmobiliaria": r"\b(?:vende|arrienda)\s+inmobiliaria\b",
    # Explicit corredor/corredora phrases
    "venta_con_corredora": r"\bventa\s+con\s+corredora?\b",
    "arriendo_con_corredora": r"\barriendo\s+con\s+corredora?\b",
    "vende_corredora": r"\bvende\s+corredora?\b",
    "arrienda_corredora": r"\barrienda\s+corredora?\b",
    "corredora_vende": r"\bcorredora?\s+vende\b",
    "corredora_arrienda": r"\bcorredora?\s+arrienda\b",
    "comision_corredor": r"\bcomisi[oó]n\s+corredora?\b",
    "intermediacion_inmobiliaria": r"\bintermediaci[oó]n\s+inmobiliaria\b",
    "asesor_inmobiliario": r"\basesor[aes]?\s+inmobiliari[oa]\b",
    "gestion_inmobiliaria": r"\bgesti[oó]n\s+inmobiliaria\b",
    "se_paga_comision": r"\bse\s+paga\s+comisi[oó]n\b",
}

# Weak/contextual professional signals: suggestive but not conclusive alone
WEAK_PROFESSIONAL_PATTERNS = {
    "agenda_visita": r"\b(?:agenda|agende|coordina)\s+(?:tu\s+)?visita\b",
    "ejecutivo": r"\bejecutivo\s+inmobiliario\b",
    "codigo_interno": r"\bc[oó]digo\s+interno\b",
    "asesoria_inmobiliaria": r"\b(?:asesor[ií]a|asesor)\s+inmobiliari[oa]\b",
}

OTHER_PROJECT_PATTERNS = {
    "ultimas_unidades": r"\bultimas?\s+unidades?\b",
    "ultimos_departamentos": r"\bultimos?\s+departamentos?\b",
    "proyecto_inmobiliario": r"\bproyecto\s+inmobiliario\b",
    "proyecto_construccion": r"\bproyecto\s+(en\s+)?construccion\b",
    "entrega_inmediata": r"\bentrega\s+inmediata\b",
    "entrega_futura": r"\bentrega\s+futura\b",
    "sala_ventas": r"\bsala\s+de\s+ventas?\b",
    "piloto": r"\bpiloto\b",
    "cotiza": r"\bcotiza\b",
    "tipologias": r"\btipologias?\b",
    "modelos_disponibles": r"\bmodelos?\s+disponibles?\b",
    "stock_disponible": r"\bstock\s+disponible\b",
    "bono_pie": r"\bbono\s+pie\b",
    "pie_en_cuotas": r"\bpie\s+en\s+cuotas?\b",
    "constructora": r"\bconstructora\b",
    "inmobiliaria": r"\binmobiliaria\b",
    "vende_proyecto": r"\bvende\s+proyecto\b",
    "departamentos_disponibles": r"\bdepartamentos?\s+disponibles?\b",
    "unidades_disponibles": r"\bunidades?\s+disponibles?\b",
    "edificio_proyecto": r"\bedificio\b.*\bproyecto\b",
    "ultimas_viviendas": r"\bultimas?\s+viviendas?\b",
    "desde_uf": r"\bdesde\s+uf\b",
    "vende_empresa": r"\bventa\s+(de\s+)?empresa\b",
}


def detect_professional_project_signals(text: str) -> list[dict]:
    """Return evidence dicts for professional/project language in text.
    Each entry includes a 'level' key: 'strong', 'weak', or 'other'."""
    evidence: list[dict] = []
    norm = normalize_text(text)
    # Check strong signals first (real estate brokerage)
    for signal_name, pattern in STRONG_PROFESSIONAL_PATTERNS.items():
        m = re.search(pattern, norm)
        if m:
            evidence.append({
                "signal": signal_name, "level": "strong",
                "pattern": str(pattern),
                "evidence": m.group(0)[:80],
            })
    # Check weak signals
    for signal_name, pattern in WEAK_PROFESSIONAL_PATTERNS.items():
        m = re.search(pattern, norm)
        if m:
            evidence.append({
                "signal": signal_name, "level": "weak",
                "pattern": str(pattern),
                "evidence": m.group(0)[:80],
            })
    # Check other project patterns
    for signal_name, pattern in OTHER_PROJECT_PATTERNS.items():
        m = re.search(pattern, norm)
        if m:
            evidence.append({
                "signal": signal_name, "level": "other",
                "pattern": str(pattern),
                "evidence": m.group(0)[:80],
            })
    return evidence


def detect_explicit_owner(extracted: dict[str, Any]) -> list[str]:
    text = normalize_text(" ".join(str(extracted.get(k, "")) for k in ("description", "descripcion", "title", "seller_text", "seller_name", "publicador_visible")))
    hits = [p for p in STRICT_EXPLICIT_OWNER_PHRASES if p in text]
    return hits


def classify_structural_owner(extracted: dict[str, Any]) -> dict[str, Any] | None:
    signals = {}
    evidence = []

    seller_type = str(extracted.get("seller_type", "")).upper()
    url_format = str(extracted.get("url_format", ""))
    owner_hits = detect_explicit_owner(extracted)

    if not owner_hits:
        return None

    signals["dueno"] = True
    signals["venta_directa"] = True
    for h in owner_hits[:3]:
        evidence.append(h)

    if seller_type == "PARTICULAR":
        signals["particular"] = True
        evidence.append("seller_type=PARTICULAR")

    if "particular" in url_format:
        signals["url_particular"] = True
        evidence.append(f"url_format={url_format}")

    strong_company = normalize_text(" ".join(str(extracted.get(k, "")) for k in (
        "seller_type_source", "company_name", "broker_brand", "listing_advertiser",
        "contact_logo_alt", "seller_jsonld_name",
    )))
    has_company_conflict = any(term in strong_company for term in (
        "inmobiliaria", "corredor", "corredora", "propiedades", "real estate", "broker",
    ))
    if has_company_conflict:
        return None

    # Check for professional project signals that conflict with the owner claim
    text_fields = " ".join(str(extracted.get(k, "")) for k in
                           ("title", "description", "descripcion", "seller_text", "seller_name",
                            "publicador_visible", "contact_name", "body_text"))
    proj_signals = detect_professional_project_signals(text_fields)
    if proj_signals:
        for ps in proj_signals:
            evidence.append(f"project_conflict:{ps['signal']} evidence={ps['evidence']}")
        return None

    return {
        "state": "DUEÑO_SEGURO",
        "confidence": 0.98,
        "score": 0.98,
        "rule_state": "DUEÑO_SEGURO",
        "signals": signals,
        "evidence": evidence[:8],
        "reason": "La descripcion declara explicitamente que la propiedad es vendida directamente por su dueno y el anunciante esta identificado como particular.",
        "decision_pattern": "explicit_owner_direct_sale",
        "decision_source": "structural_rules",
    }


def classify_obvious_broker(extracted: dict[str, Any]) -> dict[str, Any] | None:
    return is_removed_listing(extracted) or known_broker_brand_in_strong_fields(extracted) or hard_broker_terms_in_strong_fields(extracted)


def build_deepseek_context(extracted: dict[str, Any]) -> dict[str, Any]:
    context = _all_evidence_context(extracted)
    return {k: extracted.get(k, "") for k in ("publicador_visible", "contact_name", "contact_logo_alt", "seller_type", "listing_advertiser", "seller_jsonld_name", "contact_badges_text")} | {
        "company_like_suspected": context["company_like_suspected"], "company_like_evidence": context["company_like_evidence"],
        "known_brand_evidence": context["known_brand_evidence"], "hard_broker_evidence": context["hard_broker_evidence"],
        "owner_signal_evidence": context["owner_signal_evidence"], "weak_broker_evidence": context["weak_broker_evidence"],
        "descripcion": extracted.get("descripcion", extracted.get("description", "")),
    }


def build_rule_context(extracted: dict[str, Any]) -> dict[str, Any]:
    context = _all_evidence_context(extracted)
    return {**build_deepseek_context(extracted), "html_validation_status": extracted.get("html_validation_status", ""), "html_validation_reason": extracted.get("html_validation_reason", ""), "removed_listing": bool(context["removed"])}


def should_invoke_deepseek(
    rule_state: str,
    description: str,
    description_length: int = 0,
    description_is_truncated: bool = False,
    seller_type: str = "",
    has_strong_broker_rule: bool = False,
    has_explicit_owner_rule: bool = False,
) -> tuple[bool, str]:
    """Determina si DeepSeek debe ser invocado y por qué."""
    if has_strong_broker_rule:
        return False, "STRONG_RULE_FINAL"
    if has_explicit_owner_rule:
        return False, "EXPLICIT_OWNER_RULE_FINAL"
    if not description or description_length < 20:
        return False, "NO_DESCRIPTION"
    if rule_state in ("INCIERTO", "INCONCLUSIVE"):
        return True, "DEEPSEEK_REQUIRED_FOR_INCONCLUSIVE"
    return False, "NOT_REQUIRED"


def build_semantic_check(
    status: str, model: str = "", prompt_version: str = "",
    description_length: int = 0, attempts: int = 0,
    error: str = "", checked_at: str = "",
) -> dict[str, Any]:
    return {
        "status": status,  # PENDING, VALID, ERROR, NO_DESCRIPTION, SKIPPED_STRONG_RULE, SKIPPED_EXPLICIT_OWNER
        "required": status in ("PENDING", "ERROR"),
        "model": model,
        "prompt_version": prompt_version,
        "description_length": description_length,
        "attempts": attempts,
        "error": error,
        "checked_at": checked_at,
    }


def classify_with_rules(extracted: dict[str, Any]) -> dict[str, Any]:
    structural_broker = classify_structural_broker(extracted)
    if structural_broker: return structural_broker
    structural_owner = classify_structural_owner(extracted)
    if structural_owner: return structural_owner
    obvious = classify_obvious_broker(extracted)
    if obvious: return obvious
    company = company_shape_in_publisher_fields(extracted)
    return {"state": "INCONCLUSIVE", "confidence": 0.35 if company["company_like_suspected"] else 0.2, "reason": "No se encontraron senales suficientes de corredor ni de propietario.", "evidence": company["company_like_evidence"], "source": "rules_json", **company, "owner_signal_evidence": detect_owner_signals(extracted), "weak_broker_evidence": []}
