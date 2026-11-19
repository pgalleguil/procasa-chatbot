"""Verified cross-channel search demand, separate from HOT/COLD and SLA.

Only canonical office customer turns and explicitly authorized executive WhatsApp
customer turns are interpreted. No private chats, synthetic portal notices, bot
claims or agent promises can supply requirements. This module does not send.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .executive_semantics import plain, ambiguous
from .geo_utils import COMMUNE_OFFICIAL_NAMES

ROOT = "commercial_intelligence.active_search"
STATUS_ACTIVE = "ACTIVE"
STATUS_COMPLETED = "COMPLETED"

_ACTIVE = re.compile(
    r"\b(?:ando buscando|estoy buscando|sigo buscando|busco|necesito (?:una? |algo |)"
    r"(?:casa|departamento|depto|propiedad)|quiero (?:comprar|arrendar)|"
    r"otras? (?:alternativas?|opciones?)|algo mas (?:barato|economico)|"
    r"busqu(?:en|eme|enme) (?:algo|una?)|"
    r"tienen (?:algo|otra|otras) (?:mas barato|mas economico|en))\b"
)
_COMPLETED = re.compile(
    r"^(?:ya (?:encontre|compre|arrende) (?:una? )?(?:casa|depto|departamento|propiedad)"
    r"|ya no (?:busco|estoy buscando)|no quiero seguir buscando|"
    r"termine (?:la|mi) busqueda)(?:[,. ]+gracias)?$"
)
_OPT_OUT = re.compile(
    r"^(?:no me (?:escriban|escribas|contacten|contactes) mas|"
    r"no quiero (?:mas )?mensajes|denme de baja|stop)$"
)
_PROPERTY_REJECTED = re.compile(
    r"\b(?:no me sirve (?:por (?:precio|valor)|ya)|"
    r"(?:muy|demasiado) (?:car[oa]|costos[oa])|"
    r"no me alcanza (?:para (?:ese|esa)|el presupuesto))\b"
)
_AMOUNT_MILLIONS = re.compile(
    r"(?<![\d.])(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:millones?|palos?)\b"
)
_AMOUNT_CLP = re.compile(r"(?:\$|clp\s*)(\d{1,3}(?:\.\d{3}){2,}|\d{7,10})(?!\d)")
_PRICE_QUALIFIER = re.compile(
    r"\b(?:busco|buscando|presupuesto|hasta|maximo|de|por|no mas de|"
    r"quiero|necesito|puedo pagar|cuento con|tengo)\b"
)
_TYPE = (
    ("CASA", re.compile(r"\bcasa(?:s)?\b")),
    ("DEPARTAMENTO", re.compile(r"\b(?:departamentos?|deptos?)\b")),
)
_QUESTION_PROPERTY = re.compile(
    r"\b(?:prefieres?|buscas?|necesitas?)\b.*\b(?:casa|departamento|depto)\b"
)
# Referring to a property is not a new buyer preference.
_REFERENCE_QUESTION = re.compile(
    r"\b(?:de que|de cual|cual|que)\s+(?:casa|departamento|depto)\s+"
    r"(?:hablamos|hablas|es|se trata|te refieres)\b"
)
_EXCLUSIVE_TYPE = re.compile(
    r"\b(?:solo|solamente|unicamente|exclusivamente)\s+"
    r"(?:una?\s+)?(?:casas?|departamentos?|deptos?)\b"
)
# Keep the buyer's landmark in text form until an administrator verifies location.
# Never infer a commune, distance, or coordinates from an informal school name.
_PROXIMITY_TARGET = re.compile(
    r"\\b(?:busco|buscando|necesito|quiero|prefiero)\\b.{0,85}?"
    r"\\b(?:mas\\s+cerca|cerca)\\s+(?:de|del)\\s+"
    r"([a-z0-9]+(?:\\s+[a-z0-9]+){0,7})"
)
_VALID_COMMUNES = tuple(sorted(COMMUNE_OFFICIAL_NAMES.keys(), key=len, reverse=True))


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None


def _source_turn(message: dict, *, conversation_id: str, cycle_id: str,
                 owner_id: str, session_id: str) -> str | None:
    if message.get("actor_type") != "customer":
        return None
    channel = message.get("channel")
    if (channel == "executive_whatsapp"
            and str(message.get("assignment_cycle_id")) == cycle_id
            and str(message.get("assigned_executive_id")) == owner_id
            and str(message.get("wasender_session_id")) == session_id
            and bool(message.get("provider_message_id"))):
        return "executive_whatsapp"
    if (channel in (None, "", "office_whatsapp", "whatsapp")
            and message.get("role") == "user"
            and str(message.get("conversation_id") or "") == conversation_id
            and conversation_id
            and message.get("source") not in ("prop360", "crm_manual_api", "crm_hot_auto_api")
            and message.get("portal") is None):
        return "office_whatsapp"
    return None


def _max_clp(body: str, *, already_searching: bool) -> int | None:
    if not already_searching and not _PRICE_QUALIFIER.search(body):
        return None
    for match in _AMOUNT_MILLIONS.finditer(body):
        amount = int(round(float(match.group(1).replace(",", ".")) * 1_000_000))
        if 10_000_000 <= amount <= 2_000_000_000:
            return amount
    match = _AMOUNT_CLP.search(body)
    if match:
        amount = int(match.group(1).replace(".", ""))
        if 10_000_000 <= amount <= 2_000_000_000:
            return amount
    return None


def _communes(body: str) -> list[str]:
    # Only explicit municipality names supplied by the customer.  The
    # official dictionary canonicalizes accents (Maipu -> Maipú).
    found = []
    for key in _VALID_COMMUNES:
        if re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", body):
            label = COMMUNE_OFFICIAL_NAMES[key]
            if label not in found:
                found.append(label)
    return found[:8]


def capture_active_search(
    messages: list[dict], *, conversation_id: str, cycle_id: str,
    owner_id: str, session_id: str, original_property_code: str = "",
    original_operation: str = "", prior: dict | None = None,
) -> dict | None:
    """Customer-verified search with evidence IDs, but without raw transcripts.

    Explicit rejection of a specific listing is NOT completed search.
    A later thank-you does not clear requirements. Explicit opt-out never
    becomes contact permission.
    """
    records = []
    for index, row in enumerate(messages):
        if not isinstance(row, dict):
            continue
        source = _source_turn(row, conversation_id=conversation_id,
                              cycle_id=cycle_id, owner_id=owner_id, session_id=session_id)
        dt = _aware(row.get("timestamp"))
        if not source or dt is None or not isinstance(row.get("content"), str):
            continue
        records.append((dt, index, source, row))
    records.sort(key=lambda r: (r[0], r[1]))
    state = dict(prior or {})
    criteria = dict(state.get("criteria") or {})
    evidence = dict(state.get("field_evidence") or {})
    excluded = list(state.get("excluded_property_codes") or [])
    sources = list(state.get("source_channels") or [])
    status = state.get("status", "UNKNOWN")
    opt_out = bool(state.get("opt_out", False))
    newest = state.get("last_customer_at")
    explicit = status == STATUS_ACTIVE
    for timestamp, _, channel, row in records:
        body = plain(row["content"])[:700]
        if not body or ambiguous(body):
            continue
        if newest and (_aware(newest) or timestamp) > timestamp:
            continue
        message_id = str(row.get("provider_message_id") or row.get("message_id") or "")
        if not message_id:
            continue
        if _OPT_OUT.fullmatch(body.strip(" .!?")):
            opt_out = True
            continue
        if _COMPLETED.fullmatch(body.strip(" .!?")):
            status = STATUS_COMPLETED
            explicit = False
            newest = timestamp.isoformat()
            continue
        if _PROPERTY_REJECTED.search(body):
            code = str(original_property_code or "")
            if code.isdigit() and code not in excluded:
                excluded.append(code)
            # Do not close the broader search because one listing is too dear.
            newest = timestamp.isoformat()
            continue
        started = bool(_ACTIVE.search(body))
        # A subsequent short budget or location answer completes a search
        # started in the office, even without repeating "busco".
        budget = _max_clp(body, already_searching=explicit or started)
        areas = _communes(body) if explicit or started else []
        proximity = _PROXIMITY_TARGET.search(body)
        types = ([] if _REFERENCE_QUESTION.search(body) else
                 [kind for kind, pattern in _TYPE if pattern.search(body)])
        # "casa" alone is a preference correction only inside a live search;
        # do not infer searches from unrelated one-word answers.
        if started or (explicit and (budget or areas or types or proximity)):
            explicit = True
            status = STATUS_ACTIVE
            if channel not in sources:
                sources.append(channel)
            if budget:
                criteria["max_price_clp"] = budget
                evidence["max_price_clp"] = {"source": channel, "message_id": message_id}
            if areas:
                criteria["comunas"] = areas
                evidence["comunas"] = {"source": channel, "message_id": message_id}
            if proximity:
                label = proximity.group(1).strip()
                # A landmark is not a verified address and must never drive
                # direct geospatial scraping without human geocoding.
                targets = list(criteria.get("location_targets") or [])
                if label not in targets:
                    criteria["location_targets"] = (targets + [label])[-3:]
                    evidence["location_targets"] = {
                        "source": channel, "message_id": message_id,
                        "verification": "HUMAN_GEOCODING_REQUIRED",
                    }
            if types:
                prior_types = list(criteria.get("property_types") or [])
                exclusive = bool(_EXCLUSIVE_TYPE.search(body))
                # "casa porfis" after "casa o departamento" is a preference,
                # not a cancellation of the previously accepted alternative.
                if not prior_types or len(types) > 1 or exclusive:
                    accepted = types
                else:
                    accepted = list(dict.fromkeys(prior_types + types))
                if accepted != prior_types:
                    criteria["property_types"] = accepted
                    evidence["property_types"] = {"source": channel, "message_id": message_id}
                if len(types) == 1:
                    criteria["preferred_property_type"] = types[0]
                    evidence["preferred_property_type"] = {
                        "source": channel, "message_id": message_id,
                    }
                elif criteria.get("preferred_property_type") not in accepted:
                    criteria.pop("preferred_property_type", None)
                    evidence.pop("preferred_property_type", None)
            if re.search(r"\b(?:arriendo|arrendar|alquilar)\b", body):
                criteria["operation"] = "ARRIENDO"
                evidence["operation"] = {"source": channel, "message_id": message_id}
            elif re.search(r"\b(?:comprar|compra|venta)\b", body):
                criteria["operation"] = "VENTA"
                evidence["operation"] = {"source": channel, "message_id": message_id}
            newest = timestamp.isoformat()
    if not criteria and status != STATUS_COMPLETED:
        return None
    if status == STATUS_ACTIVE and not criteria.get("operation"):
        # The prospect came from a sale listing, so a buyer search can
        # inherit the transaction type, never a budget or a location.
        if plain(original_operation) in ("venta", "v"):
            criteria["operation"] = "VENTA"
            evidence["operation"] = {"source": "original_listing", "message_id": None}
        elif plain(original_operation) in ("arriendo", "a"):
            criteria["operation"] = "ARRIENDO"
            evidence["operation"] = {"source": "original_listing", "message_id": None}
    return {
        "status": status, "criteria": criteria,
        "field_evidence": evidence, "source_channels": sources,
        "excluded_property_codes": excluded[:12],
        "last_customer_at": newest, "opt_out": opt_out,
        "followup_required": (status == STATUS_ACTIVE
                              and ((bool(criteria.get("max_price_clp"))
                                    and bool(criteria.get("comunas")))
                                   or bool(criteria.get("location_targets")))),
    }


def find_matching_inventory(db, search: dict, *, limit: int = 8) -> list[dict]:
    """Exact, active, published matches; return candidates, never send.

    A UF publication's CLP amount may be a stale conversion. The returned
    matches require a fresh price/availability check before outbound offers.
    """
    criteria = search.get("criteria") or {}
    if (search.get("status") != STATUS_ACTIVE
            or criteria.get("operation") != "VENTA"
            or not criteria.get("comunas")
            or not criteria.get("max_price_clp")):
        return []
    max_clp = criteria["max_price_clp"]
    if not isinstance(max_clp, int) or not (1_000_000 <= max_clp <= 2_000_000_000):
        return []
    excluded = set(str(x) for x in search.get("excluded_property_codes") or [])
    filt = {
        "disponible_prop360": True,
        "tipo_operacion.venta": True,
        "estado.estado_prop360": "Activa",
        "ubicacion.comuna": {"$in": criteria["comunas"]},
        "tipo_operacion.precio_venta.precio_clp": {"$gt": 0, "$lte": max_clp},
        "estado.oficina": {"$in": ["PROCASA SUCRE", "INMOBILIARIA SUCRE SPA"]},
        "publicaciones.procasa.publicaciones.V.publicada": True,
    }
    kinds = criteria.get("property_types") or []
    if kinds:
        filt["tipo_operacion.tipo"] = {"$in": [
            "Casa" if x == "CASA" else "Departamento"
            for x in kinds if x in ("CASA", "DEPARTAMENTO")
        ]}
        if not filt["tipo_operacion.tipo"]["$in"]:
            return []
    fields = {
        "_id": 0, "codigo": 1, "tipo_operacion.tipo": 1,
        "tipo_operacion.precio_venta.precio_clp": 1,
        "tipo_operacion.precio_venta.precio_uf": 1,
        "tipo_operacion.precio_venta.moneda_publicada": 1,
        "ubicacion.comuna": 1,
        "publicaciones.procasa.url_procasa": 1,
    }
    found = []
    for p in db["universo_cartera_prop360"].find(filt, fields).sort(
        "tipo_operacion.precio_venta.precio_clp", 1
    ).limit(max(1, min(limit + len(excluded), 30))):
        code = str(p.get("codigo") or "")
        pub = (p.get("publicaciones") or {}).get("procasa") or {}
        link = pub.get("url_procasa")
        price = ((p.get("tipo_operacion") or {}).get("precio_venta") or {})
        if code in excluded or not re.fullmatch(r"\d{3,8}", code):
            continue
        if not isinstance(link, str) or link != f"https://www.procasa.cl/{code}":
            continue
        found.append({
            "property_code": code,
            "property_type": ((p.get("tipo_operacion") or {}).get("tipo") or ""),
            "comuna": ((p.get("ubicacion") or {}).get("comuna") or ""),
            "price_clp_reference": price.get("precio_clp"),
            "price_uf": price.get("precio_uf"),
            "public_url": link,
            "verification": "RECHECK_PRICE_AND_AVAILABILITY",
        })
        if len(found) >= limit:
            break
    return found
