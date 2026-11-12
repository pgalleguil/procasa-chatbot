"""Read-only discovery of current Prop360 price reductions and exact inquiry leads."""
from __future__ import annotations
import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import price_drop_leads_v2 as v2
from .price_drop_campaign_filters import (
    KNOWN_TEMPERATURES, effective_temperature, lead_age_details,
    normalize_lead_max_age_days, normalize_temperature_filter,
    temperature_filter_matches,
)
from .phone_utils import has_real_phone, normalize_phone_strict, phone_identity_variants
from .price_drop_campaign_classifier import lead_must_not_be_contacted

CHILE_TZ = ZoneInfo("America/Santiago")
HISTORY_COLLECTION = "universo_cartera_prop360_historial"
PROPERTY_COLLECTION = "universo_cartera_prop360"
LEADS_COLLECTION = "leads"


class DiscoveryError(ValueError):
    pass


@dataclass(frozen=True)
class PriceDropEvent:
    event_id: str
    campaign_id: str
    property_code: str
    property_type: str
    comuna: str
    operation: str
    dropped_at: datetime
    old_price: int | float
    new_price: int | float
    currency: str
    difference: int | float
    percent_reduction: float
    available: bool
    published: bool
    current_price: int | float
    listing_url: str
    history_id: str
    prior_event_ids: tuple[str, ...] = ()

    def as_dict(self):
        row = asdict(self)
        row["dropped_at"] = self.dropped_at.isoformat()
        row["prior_event_ids"] = list(self.prior_event_ids)
        return row


def parse_aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return (parsed.astimezone(timezone.utc)
            if parsed.tzinfo is not None and parsed.utcoffset() is not None else None)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def stable_event_id(code: str, dropped_at: datetime, currency: str,
                    old: int | float | Decimal, new: int | float | Decimal) -> str:
    stamp, before, after = parse_aware_datetime(dropped_at), _decimal(old), _decimal(new)
    code, currency = str(code or "").strip(), str(currency or "").upper()
    if not code or stamp is None or currency not in {"UF", "CLP"} or before is None or after is None:
        raise DiscoveryError("event_identity_fields_invalid")
    semantic_key = "|".join((code, stamp.isoformat(), currency,
                             format(before.normalize(), "f"), format(after.normalize(), "f")))
    digest = hashlib.sha256(semantic_key.encode("utf-8")).hexdigest()[:12]
    return f"rebaja_{code}_{stamp.strftime('%Y%m%dT%H%M%S')}_{currency}_{digest}"


def _walk_publications(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if any(key in value for key in ("publicada", "publication_status", "url")):
            yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk_publications(child)
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, (dict, list)):
                yield from _walk_publications(child)


def _published(prop: dict[str, Any], now: datetime) -> tuple[bool, str]:
    for row in _walk_publications(prop.get("publicaciones") or {}):
        status = str(row.get("publication_status") or "").casefold()
        state = row.get("estado")
        if not row.get("publicada") or not (
            state in (1, "1", "ACTIVA", "PUBLICADA", "ACTIVE") or status == "active"
        ):
            continue
        operation = str(row.get("operacion") or "").casefold()
        if operation and operation not in {"venta", "v", "sell"}:
            continue
        url = str(row.get("url") or "").strip()
        expiry = row.get("expiration_date")
        if expiry:
            end = parse_aware_datetime(expiry)
            if end is None or end <= now:
                continue
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.scheme == "https" and parsed.netloc:
            return True, url
    return False, ""


def _comuna(prop: dict[str, Any]) -> str:
    return str(((prop.get("ubicacion") or {}).get("comuna")
                or (prop.get("direccion") or {}).get("comuna")
                or prop.get("comuna")
                or (prop.get("resumen") or {}).get("comuna") or "N/A"))


def normalize_price_event(history: dict[str, Any], prop: dict[str, Any],
                          *, now: datetime | None = None) -> PriceDropEvent | None:
    if not isinstance(history, dict) or not isinstance(prop, dict) or history.get("campo") != "precio_publicado":
        return None
    code = str(history.get("codigo") or "").strip()
    if not re.fullmatch(r"\d{4,6}", code) or str(prop.get("codigo") or "") != code:
        return None
    old_value, new_value = history.get("valor_anterior") or {}, history.get("valor_nuevo") or {}
    old_currency = str(history.get("moneda_anterior") or old_value.get("moneda") or "").upper()
    new_currency = str(history.get("moneda_nueva") or new_value.get("moneda") or "").upper()
    if old_currency not in {"UF", "CLP"} or old_currency != new_currency:
        return None
    old, new = _decimal(history.get("monto_anterior")), _decimal(history.get("monto_nuevo"))
    old, new = old or _decimal(old_value.get("monto")), new or _decimal(new_value.get("monto"))
    if old is None or new is None or not old > new > 0:
        return None
    dropped_at = parse_aware_datetime(history.get("fecha"))
    current_time = parse_aware_datetime(now or datetime.now(timezone.utc))
    if dropped_at is None or current_time is None or dropped_at > current_time:
        return None
    op = prop.get("tipo_operacion") or {}
    pricing = op.get("precio_venta") or {}
    currency_now = str(pricing.get("moneda_publicada") or pricing.get("moneda_publicacion") or "").upper()
    current_price = _decimal(pricing.get("precio_publicado"))
    if currency_now != old_currency or current_price != new:
        return None
    if op.get("venta") is not True or prop.get("disponible_prop360") is not True:
        return None
    if (prop.get("estado") or {}).get("oficina") != "PROCASA SUCRE":
        return None
    published, url = _published(prop, current_time)
    if not published:
        return None
    event_id = stable_event_id(code, dropped_at, old_currency, old, new)
    delta = old - new
    return PriceDropEvent(
        event_id, event_id, code, str(op.get("tipo") or "N/A"), _comuna(prop),
        "Venta", dropped_at, _number(old), _number(new), old_currency, _number(delta),
        float(delta / old * Decimal("100")), True, True, _number(current_price),
        url, str(history.get("_id") or ""),
    )


def _embedded_current_price_drop(prop: dict[str, Any], *,
                                 now: datetime | None = None,
                                 since: datetime | None = None,
                                 currency: str | None = None) -> PriceDropEvent | None:
    """Use the original timestamped Prop360 price history when the central
    history collection has no eligible event for this property.

    Only original sale-price changes (precio_uf/precio_clp) are accepted.
    Derived/converted fields and price-change requests are not evidence of a
    confirmed reduction. normalize_price_event rechecks the current price,
    currency, sale availability, office and published HTTPS listing.
    """
    matches: list[PriceDropEvent] = []
    code = str(prop.get("codigo") or "")
    for row in prop.get("historial_cambios") or []:
        if not isinstance(row, dict):
            continue
        field = str(row.get("campo") or "")
        if field not in {"precio_uf", "precio_clp"}:
            continue
        historical_currency = "UF" if field == "precio_uf" else "CLP"
        if currency and historical_currency != currency:
            continue
        event = normalize_price_event({
            "campo": "precio_publicado",
            "codigo": code,
            "fecha": row.get("fecha"),
            "moneda_anterior": historical_currency,
            "moneda_nueva": historical_currency,
            "monto_anterior": row.get("valor_anterior"),
            "monto_nuevo": row.get("valor_nuevo"),
            "_id": f"embedded:{code}:{field}:{row.get('fecha')}",
        }, prop, now=now)
        if event and (since is None or event.dropped_at >= since):
            matches.append(event)
    if not matches:
        return None
    # One active price window per property. Never target a customer with two
    # simultaneous campaigns because different historical steps ended at the
    # same current asking price.
    return max(matches, key=lambda event: (
        event.dropped_at, event.event_id,
    ))


def discover_price_drops(db: Any, *, since: datetime | None = None,
                         limit: int | None = None, now: datetime | None = None,
                         property_code: str | None = None,
                         operation: str | None = None,
                         currency: str | None = None,
                         batch_size: int = 500) -> list[PriceDropEvent]:
    """Read price history in bounded batches; ``limit=None`` means full scan.

    A complete scan must not stop at an arbitrary number of Mongo documents.
    Only one history page and its current property documents are kept in memory
    at a time; returned events are the validated, deduplicated result set.
    """
    if limit is not None and (type(limit) is not int or limit < 1):
        raise DiscoveryError("limit_must_be_positive_or_unset")
    if type(batch_size) is not int or not 1 <= batch_size <= 5000:
        raise DiscoveryError("batch_size_must_be_between_1_and_5000")
    since_utc = parse_aware_datetime(since) if since is not None else None
    query: dict[str, Any] = {"campo": "precio_publicado"}
    if property_code:
        query["codigo"] = str(property_code)
    if since_utc:
        # Prop360 history is normally BSON datetime; fixtures and older imports
        # can contain ISO strings, so keep both representations in the query.
        query["$or"] = [
            {"fecha": {"$gte": since_utc}},
            {"fecha": {"$gte": since_utc.isoformat()}},
        ]
    currency_filter = str(currency or "").strip().upper()
    if currency_filter:
        if currency_filter not in {"UF", "CLP"}:
            raise DiscoveryError("currency_must_be_UF_or_CLP")
        query["$and"] = [
            {"$or": [{"moneda_anterior": currency_filter},
                     {"valor_anterior.moneda": currency_filter}]},
            {"$or": [{"moneda_nueva": currency_filter},
                     {"valor_nuevo.moneda": currency_filter}]},
        ]
    histories = db[HISTORY_COLLECTION].find(
        query,
        {"_id": 1, "codigo": 1, "campo": 1, "fecha": 1, "moneda_anterior": 1,
         "moneda_nueva": 1, "monto_anterior": 1, "monto_nuevo": 1,
         "valor_anterior": 1, "valor_nuevo": 1},
    ).sort([("fecha", -1), ("_id", -1)])
    if limit is not None:
        histories = histories.limit(limit)
    if hasattr(histories, "batch_size"):
        histories = histories.batch_size(batch_size)

    property_projection = {
        "_id": 0, "codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
        "tipo_operacion": 1, "publicaciones": 1, "ubicacion.comuna": 1,
        "direccion.comuna": 1, "comuna": 1, "resumen.comuna": 1,
    }
    events, seen = {}, set()
    history_iter = iter(histories)
    while True:
        batch = []
        for _ in range(batch_size):
            try:
                batch.append(next(history_iter))
            except StopIteration:
                break
        if not batch:
            break
        codes = {str(row.get("codigo") or "") for row in batch}
        properties = {
            code: db[PROPERTY_COLLECTION].find_one({"codigo": code}, property_projection) or {}
            for code in codes if code
        }
        for history in batch:
            code = str(history.get("codigo") or "")
            prop = properties.get(code, {})
            event = normalize_price_event(history, prop, now=now)
            if event is None or (since_utc and event.dropped_at < since_utc):
                continue
            if operation and event.operation.casefold() != str(operation).strip().casefold():
                continue
            if currency_filter and event.currency != currency_filter:
                continue
            key = (event.property_code, event.dropped_at.isoformat(), event.currency,
                   str(event.old_price), str(event.new_price))
            if key in seen:
                continue
            seen.add(key)
            events[event.event_id] = event
    # The central price-history collection is not a complete historic
    # backfill. Prop360 also stores genuine, dated price changes in each
    # property's historial_cambios array. Include those missing property
    # windows without altering identities of centrally recorded campaigns.
    # A diagnostic --limit intentionally examines only central history rows.
    if limit is None:
        embedded_query: dict[str, Any] = {
            "estado.oficina": "PROCASA SUCRE",
            "tipo_operacion.venta": True,
            "disponible_prop360": True,
        }
        if property_code:
            embedded_query["codigo"] = str(property_code)
        embedded_projection = {
            **property_projection, "historial_cambios": 1,
        }
        properties_with_event = {
            event.property_code for event in events.values()
        }
        cursor = db[PROPERTY_COLLECTION].find(
            embedded_query, embedded_projection,
        )
        if hasattr(cursor, "batch_size"):
            cursor = cursor.batch_size(batch_size)
        for prop in cursor:
            code = str(prop.get("codigo") or "")
            if code in properties_with_event:
                continue
            extra = _embedded_current_price_drop(
                prop, now=now, since=since_utc, currency=currency_filter or None,
            )
            if extra is None:
                continue
            if operation and extra.operation.casefold() != str(operation).strip().casefold():
                continue
            events[extra.event_id] = extra
            properties_with_event.add(code)
    return sorted(events.values(), key=lambda item: (item.dropped_at, item.property_code, item.event_id))


def _candidate_query(event: PriceDropEvent, listing_ids: set[str]) -> dict[str, Any]:
    patterns = []
    for ref in {event.property_code, *listing_ids}:
        ref = str(ref or "").strip()
        if not ref:
            continue
        if ref.upper().startswith("MLC-"):
            pattern = r"(?<![A-Za-z0-9])MLC-?" + re.escape(ref[4:]) + r"(?![A-Za-z0-9])"
        else:
            pattern = r"(?<![A-Za-z0-9])" + re.escape(ref) + r"(?![A-Za-z0-9])"
        patterns.append(pattern)
    regex = "(?:" + "|".join(patterns) + ")" if patterns else r"(?!)"
    return {"messages": {"$elemMatch": {
        "role": {"$in": ["user", "customer"]},
        "$or": [{"metadata.property_code": event.property_code},
                {"metadata.codigo_propiedad": event.property_code},
                {"metadata.codigo": event.property_code},
                {"content": {"$regex": regex, "$options": "i"}}],
    }}}


def _rows(lead: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in ((lead.get("price_drop_v2") or {}).get("campaigns") or [])
            if isinstance(row, dict)]


def _property_rows(lead: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [row for row in _rows(lead) if str(row.get("property_code") or "") == code]


def _block_reason(lead: dict[str, Any], event: PriceDropEvent) -> str | None:
    for row in _rows(lead):
        if str(row.get("property_code") or "") != event.property_code:
            continue
        existing_events = {
            str(value) for value in (
                row.get("event_id"), *(row.get("history_event_ids") or [])
            ) if value
        }
        current_events = {event.event_id, *event.prior_event_ids}
        if row.get("campaign_id") == event.event_id:
            duplicate = True
        elif existing_events:
            duplicate = bool(existing_events.intersection(current_events))
        else:
            old_price = (row.get("old_price_clp") if event.currency == "CLP"
                         else row.get("old_price_uf"))
            new_price = (row.get("new_price_clp") if event.currency == "CLP"
                         else row.get("new_price_uf"))
            row_currency = str(row.get("currency") or (
                "CLP" if row.get("old_price_clp") is not None else "UF"
            )).upper()
            duplicate = (row_currency == event.currency
                         and old_price == event.old_price
                         and new_price == event.new_price)
        if duplicate:
            status = str(row.get("status") or "").upper()
            return ("event_already_attempted" if status in {"SENT", "RESPONDED"}
                    else "event_delivery_unresolved" if status in {"SENDING", "DELIVERY_UNKNOWN"}
                    else "event_already_prepared")
    for message in lead.get("messages") or []:
        meta = message.get("metadata") or {} if isinstance(message, dict) else {}
        if (meta.get("campaign_id") == event.event_id
                or meta.get("price_drop_event_id") == event.event_id
                or event.event_id in (meta.get("history_event_ids") or [])):
            return "event_already_in_message_history"
        if (str(meta.get("property_code") or meta.get("codigo_propiedad") or "")
                == event.property_code):
            currency = str(meta.get("currency") or "").upper()
            old = meta.get("old_price_clp") if event.currency == "CLP" else meta.get("old_price_uf")
            new = meta.get("new_price_clp") if event.currency == "CLP" else meta.get("new_price_uf")
            if currency == event.currency and old == event.old_price and new == event.new_price:
                return "event_already_in_message_history"
        if isinstance(message, dict) and message.get("role") in {"assistant", "agent", "human"}:
            content = str(message.get("content") or "")
            def rendered(value):
                return f"{int(value):,}".replace(",", ".") if float(value).is_integer() else str(value)
            if (event.property_code in content
                    and rendered(event.old_price) in content
                    and rendered(event.new_price) in content
                    and ("UF" in content.upper() if event.currency == "UF"
                         else "$" in content)):
                return "prior_price_drop_message_needs_review"
    same = _property_rows(lead, event.property_code)
    if any(row.get("status") == "DECLINED" for row in same):
        return "property_declined"
    if any(row.get("status") in {"SENDING", "DELIVERY_UNKNOWN"} for row in same):
        return "unresolved_previous_delivery"
    if any(row.get("status") in {"PREPARED", "APPROVED"} for row in same):
        return "active_campaign_for_property"
    return None


def _eligibility(db: Any, lead: dict[str, Any], event: PriceDropEvent,
                 listing_ids: set[str]) -> tuple[bool, str, dict[str, Any] | None]:
    phone = normalize_phone_strict(lead.get("contact_phone_normalized") or lead.get("phone") or "")
    if not phone or not has_real_phone(phone):
        return False, "invalid_phone", None
    if lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete"):
        return False, "identity_incomplete", None
    if lead_must_not_be_contacted(lead):
        return False, "opted_out_or_search_complete", None
    if not v2.is_open_campaign_eligible(lead):
        return False, "closed_opportunity", None
    reason = _block_reason(lead, event)
    if reason:
        return False, reason, None
    variants = phone_identity_variants(phone)
    duplicates = list(db[LEADS_COLLECTION].find(
        {"$or": [{"phone": {"$in": list(variants)}},
                 {"contact_phone_normalized": {"$in": list(variants)}}]},
        {"_id": 1},
    ).limit(2))
    if len(duplicates) != 1 or str(duplicates[0].get("_id")) != str(lead.get("_id")):
        return False, "duplicate_phone", None
    evidence = v2._inquiry_before(
        lead, event.property_code, event.dropped_at, listing_ids, return_evidence=True,
    )
    if not evidence:
        return False, "no_dated_property_inquiry_before_drop", None
    temp = str(lead.get("lead_temperature_effective") or lead.get("lead_temperature") or "").strip().upper()
    if temp not in {"HOT", "NORMAL", "COLD"}:
        return True, "eligible_temperature_review_pending", evidence
    return True, "eligible", evidence


def _verified_first_name(lead: dict[str, Any]) -> str | None:
    prospect = lead.get("prospecto") or {}
    if not isinstance(prospect, dict):
        prospect = {}
    raw = prospect.get("nombre") or lead.get("nombre")
    if not raw:
        return None
    try:
        from .crm_message_context import _is_valid_client_name, title_case_name
        if not _is_valid_client_name(str(raw)):
            return None
        normalized = title_case_name(str(raw)) or ""
    except Exception:
        return None
    first = normalized.split()[0] if normalized.split() else ""
    forbidden = {"ejecutivo", "ejecutiva", "captador", "captadora", "propietario",
                 "propietaria", "asesor", "asesora", "corredor", "corredora",
                 "vendedor", "vendedora", "sistema", "cliente", "desconocido"}
    if (not first or first.casefold() in forbidden
            or not any(char.isalpha() for char in first)
            or any(not (char.isalpha() or char in "-'’") for char in first)):
        return None
    return first


def find_candidates_for_event(
    db: Any, event: PriceDropEvent, *, temperature_filter: str = "ALL",
    lead_max_age_days: int | None = None,
    temperature_reviews: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    selected_temperatures = normalize_temperature_filter(temperature_filter)
    age_limit = normalize_lead_max_age_days(lead_max_age_days)
    stamp = now or datetime.now(timezone.utc)
    reviews = temperature_reviews or {}
    listing_ids = v2._verified_listing_ids(db, event.property_code)
    leads = db[LEADS_COLLECTION].find(
        _candidate_query(event, listing_ids),
        {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
         "phone_is_synthetic": 1, "contact_identity_incomplete": 1,
         "messages": 1, "lead_temperature_effective": 1, "lead_temperature": 1,
         "created_at": 1, "nombre": 1, "prospecto.nombre": 1,
         "pipeline_stage": 1, "stage": 1, "conversation_status": 1,
         "price_drop_v2": 1, "price_drop_campaign_v1": 1},
    )
    result = []
    for lead in leads:
        base_eligible, base_reason, evidence = _eligibility(db, lead, event, listing_ids)
        raw_temperature = str(lead.get("lead_temperature_effective") or
                              lead.get("lead_temperature") or "").strip().upper()
        review = reviews.get(str(lead.get("_id") or ""))
        effective_temp = effective_temperature(lead, review)
        age = lead_age_details(lead, now=stamp, max_age_days=age_limit)
        temp_matches = temperature_filter_matches(effective_temp, selected_temperatures)
        review_required = effective_temp == "REVISIÓN_PENDIENTE"
        age_review_required = age["lead_age_status"] != "valid"
        too_old = age["excluded_by_age"]
        eligible = bool(base_eligible and not age_review_required and not too_old
                        and not review_required and temp_matches)
        age_excluded_only = bool(base_eligible and too_old and temp_matches and not review_required)
        if not base_eligible:
            reason = base_reason
        elif age_review_required:
            reason = age["lead_age_status"]
        elif too_old:
            reason = "lead_age_exceeded"
        elif review_required:
            reason = "temperature_review_pending"
        elif not temp_matches:
            reason = "temperature_filter_mismatch"
        elif raw_temperature not in KNOWN_TEMPERATURES:
            reason = "eligible_after_manual_temperature_review"
        else:
            reason = "eligible"
        property_rows = _property_rows(lead, event.property_code)
        sent = [row for row in property_rows if row.get("status") in {"SENT", "RESPONDED"}
                and isinstance(row.get("sent_at"), datetime)]
        last = max(sent, key=lambda row: row["sent_at"]) if sent else None
        phone = lead.get("contact_phone_normalized") or lead.get("phone") or ""
        temperature = raw_temperature if raw_temperature in {"HOT", "NORMAL", "COLD"} else "REVISIÓN_PENDIENTE"
        age_review = (age["lead_age_status"] if age_review_required else None)
        result.append({
            "lead_id": str(lead.get("_id") or ""), "phone_last4": str(phone)[-4:],
            "temperature": temperature, "effective_temperature": effective_temp,
            "first_name": _verified_first_name(lead),
            "created_at": age["created_at"], "lead_age_days": age["lead_age_days"],
            "lead_age_status": age["lead_age_status"],
            "temperature_review_required": review_required,
            "age_review_required": age_review_required,
            "review_flags": [flag for flag in (age_review, "temperature_review_pending"
                              if review_required else None) if flag],
            "excluded_by_age": age_excluded_only,
            "temperature_review": review,
            "inquiry_at": evidence.get("inquiry_at") if evidence else None,
            "inquiry_reference": evidence.get("reference") if evidence else None,
            "campaign_history": [{"campaign_id": r.get("campaign_id"),
                                  "status": r.get("status"), "sent_at": r.get("sent_at")}
                                 for r in property_rows],
            "last_notice": ({"campaign_id": last.get("campaign_id"), "sent_at": last.get("sent_at"),
                             "price": last.get("new_price_clp") if event.currency == "CLP"
                                      else last.get("new_price_uf")} if last else None),
            "eligible": eligible, "base_eligible": base_eligible,
            "reason": reason, "temperature_filter": selected_temperatures,
            "lead_max_age_days": age_limit,
        })
    return sorted(result, key=lambda row: (
        not row["eligible"],
        {"HOT": 0, "NORMAL": 1, "COLD": 2}.get(row["effective_temperature"], 3),
        row["inquiry_at"] or datetime.max.replace(tzinfo=timezone.utc), row["lead_id"],
    ))


def format_price(value: int | float, currency: str) -> str:
    integer = float(value).is_integer()
    rendered = f"{int(value):,}".replace(",", ".") if integer else str(value)
    return ("UF " + rendered) if currency == "UF" else (chr(36) + rendered)


MESSAGE_VARIANTS = {
    "rebaja_v1": {
        "opening": "Te escribimos de PROCASA porque consultaste por la propiedad código {code}.",
        "question": "¿Te gustaría conocer más sobre esta propiedad?",
    },
    "rebaja_v2": {
        "opening": "En PROCASA retomamos tu consulta por la propiedad código {code} para compartirte su precio actualizado.",
        "question": "¿Quieres que te contemos más sobre la propiedad?",
    },
    "rebaja_v3": {
        "opening": "Vimos tu consulta anterior por la propiedad código {code}. Te contactamos desde PROCASA porque su precio cambió.",
        "question": "¿Te interesa revisar esta alternativa?",
    },
}


def message_variant_id(lead_id: str, event_id: str) -> str:
    key = f"{str(lead_id)}|{str(event_id)}".encode("utf-8")
    index = int(hashlib.sha256(key).hexdigest()[:8], 16) % len(MESSAGE_VARIANTS)
    return tuple(MESSAGE_VARIANTS)[index]


def event_message(event: PriceDropEvent, *, last_notice: dict[str, Any] | None = None,
                  history: list[PriceDropEvent] | None = None,
                  recipient_name: str | None = None,
                  variant_id: str = "rebaja_v1") -> str:
    if variant_id not in MESSAGE_VARIANTS:
        raise DiscoveryError("message_variant_id_invalid")
    rows = sorted(history or [event], key=lambda row: row.dropped_at)
    rows = [row for row in rows if row.dropped_at <= event.dropped_at]
    changes = " y ".join(
        f"{r.dropped_at.astimezone(CHILE_TZ).strftime('%d/%m/%Y')}: "
        f"{format_price(r.old_price, r.currency)} a {format_price(r.new_price, r.currency)}"
        for r in rows
    )
    prior = (last_notice or {}).get("price")
    first = str(recipient_name or "").strip()
    greeting = f"Hola, {first}." if first else "Hola, ¿cómo estás?"
    opening = MESSAGE_VARIANTS[variant_id]["opening"].format(code=event.property_code)
    if prior is not None:
        opening += f" Desde el aviso cuando estaba en {format_price(prior, event.currency)}, "
    else:
        opening += " El historial registra estos cambios: "
    reduction = f"{format_price(event.difference, event.currency)} menos"
    return (
        greeting + " 😊\n\n" + opening + changes + ". "
        f"La última rebaja fue de {format_price(event.old_price, event.currency)} a "
        f"{format_price(event.new_price, event.currency)}, es decir, {reduction}. "
        f"El precio publicado hoy es {format_price(event.current_price, event.currency)}.\n\n"
        f"Publicación: {event.listing_url}\n\n"
        f"{MESSAGE_VARIANTS[variant_id]['question']} "
        "Si prefieres no recibir más avisos, dínoslo."
    )
