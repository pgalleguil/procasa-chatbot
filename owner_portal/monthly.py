"""Read-only monthly owner-portal view assembled from verified snapshots.

The campaign ledger and sent-email artifacts are immutable historical sources.
New monthly records may be stored in ``owner_property_portals`` without
changing either source or the registered portal access tokens.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

from .property_media import verified_historical_media, verified_media_for_property


OWNER_PROPERTY_PORTAL_COLLECTION = "owner_property_portals"
MARKET_CONTEXT_SNAPSHOT_COLLECTION = "market_context_snapshots"
MONTHLY_SNAPSHOT_SCHEMA_VERSION = 1
_TARGET_CLASSES = frozenset({
    "activity-stat-value-single",
    "activity-copy-single",
    "evidence-badge-single",
    "position-anchor-label",
    "position-ref-box",
    "position-prop-box",
    "comparable-summary-single",
    "market-reference-compact-single",
    "diagnostic-copy",
    "recommendation-title",
    "recommendation-copy",
    "report-date-single",
    "macro-heading-single",
    "macro-kpi-label-single",
    "macro-kpi-value-single",
    "macro-kpi-note-single",
    "macro-highlight-single",
    "macro-copy-single",
    "macro-source-single",
    "single-heading",
    "single-operation",
})
_NUMBER = re.compile(r"(?<![\w])(-?\d{1,3}(?:\.\d{3})*(?:,\d+)?|-?\d+(?:,\d+)?)(?![\w])")
logger = logging.getLogger(__name__)


def owner_property_portal_id(property_code: Any, owner_email: Any) -> str:
    """Stable, non-PII key for one owner/property portal."""
    code = str(property_code or "").strip()
    email = str(owner_email or "").strip().casefold()
    if not code or not email:
        raise ValueError("owner_property_portal_identity_missing")
    digest = hashlib.sha256(f"{email}\0{code}".encode("utf-8")).hexdigest()
    return f"opp1_{digest}"


def _support_document_link_is_safe(value: Any, reference_url: Any, *, campaign_id: str,
                                   property_code: str, owner_email: str, document_type: str) -> bool:
    """Accept only an existing signed report route, on the same origin as the current report."""
    candidate = urlsplit(str(value or ""))
    reference = urlsplit(str(reference_url or ""))
    tokens = parse_qs(candidate.query).get("token") or []
    if candidate.path != "/campana/informe" or len(tokens) != 1:
        return False
    from campanas.owner_campaign_live_events import decode_live_token
    claims = decode_live_token(tokens[0])
    if not claims or (
        claims.get("action") != "ver_informe"
        or claims.get("campaign_id") != campaign_id
        or str(claims.get("property_code")) != property_code
        or claims.get("recipient") != owner_email.strip().casefold()
        or claims.get("document_type") != document_type
    ):
        return False
    if not candidate.scheme and not candidate.netloc:
        return candidate.path.startswith("/")
    return bool(
        reference.scheme and reference.netloc
        and candidate.scheme == reference.scheme
        and candidate.netloc.casefold() == reference.netloc.casefold()
    )


def _support_document_date(item: Mapping[str, Any]) -> str:
    # Only source/issue dates describe the document; generated_at is not provenance.
    for key in ("source_date", "issued_at", "issued_on", "issue_date", "document_date", "appraisal_date"):
        value = item.get(key)
        if isinstance(value, (datetime, date)):
            return _source_date_label(value)
        raw = _text(value)
        if not raw:
            continue
        try:
            parsed = date.fromisoformat(raw[:10]) if re.match(r"^\d{4}-\d{2}-\d{2}", raw) else None
            if parsed:
                return parsed.strftime("%d-%m-%Y")
            for date_format in ("%d-%m-%Y", "%d/%m/%Y"):
                try:
                    return datetime.strptime(raw, date_format).strftime("%d-%m-%Y")
                except ValueError:
                    continue
        except ValueError:
            continue
    return ""


def _support_document_item(document_type: str, property_code: str, commune: str,
                           url: str, metadata: Mapping[str, Any] | None = None) -> dict[str, str]:
    metadata = metadata if isinstance(metadata, Mapping) else {}
    if document_type == "COMMUNAL_MARKET_REPORT":
        title = "Informe de mercado comunal"
        parts = [commune, "PDF"] if commune else ["PDF"]
        date_prefix = "Actualizado"
    elif document_type == "INDIVIDUAL_APPRAISAL":
        title = "Tasación comercial"
        parts = [f"Propiedad {property_code}", "PDF"]
        date_prefix = "Emitida"
    else:
        return {}
    source_date = _support_document_date(metadata)
    if source_date:
        parts.append(f"{date_prefix} {source_date}")
    elif document_type == "INDIVIDUAL_APPRAISAL":
        file_date = _file_date_label(metadata.get("file_modified_at"))
        if file_date:
            parts.append(f"Archivo actualizado {file_date}")
    return {"type": document_type, "title": title, "metadata": " · ".join(parts), "url": url}


def _file_date_label(value: Any) -> str:
    """Format a Drive file timestamp without presenting it as appraisal issue date."""
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = _text(value)
        if not raw:
            return ""
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%d/%m/%Y")


def _strict_appraisal_number(value: Any) -> float | None:
    """Parse an explicit numeric appraisal field, never a display label or prose."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        parsed = float(value)
    else:
        raw = _text(value).strip()
        if not re.fullmatch(r"\d+(?:[.,]\d+)?", raw):
            return None
        parsed = float(raw.replace(",", "."))
    import math
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _appraisal_source_maps(monthly: Mapping[str, Any], snapshot: Mapping[str, Any],
                           campaign_view: Mapping[str, Any],
                           extracted_appraisal: Mapping[str, Any] | None = None) -> list[Mapping[str, Any]]:
    """Return property-scoped appraisal maps in the approved source priority."""
    sources: list[Mapping[str, Any]] = []
    if isinstance(extracted_appraisal, Mapping):
        sources.append(extracted_appraisal)
    for container in (monthly,):
        for key in ("individual_appraisal", "appraisal"):
            value = container.get(key)
            if isinstance(value, Mapping):
                sources.append(value)
        evidence = container.get("supporting_evidence")
        if isinstance(evidence, Mapping) and isinstance(evidence.get("appraisal"), Mapping):
            sources.append(evidence["appraisal"])
        nested_evidence = container.get("evidence")
        nested_support = nested_evidence.get("supporting_evidence") if isinstance(nested_evidence, Mapping) else None
        if isinstance(nested_support, Mapping) and isinstance(nested_support.get("appraisal"), Mapping):
            sources.append(nested_support["appraisal"])

    for container in (snapshot, campaign_view.get("snapshot") if isinstance(campaign_view.get("snapshot"), Mapping) else {}):
        for key in ("individual_appraisal", "appraisal"):
            value = container.get(key)
            if isinstance(value, Mapping):
                sources.append(value)
        evidence = container.get("supporting_evidence")
        if isinstance(evidence, Mapping) and isinstance(evidence.get("appraisal"), Mapping):
            sources.append(evidence["appraisal"])
        for model_key in ("email_render_model", "render_model"):
            model = container.get(model_key)
            if not isinstance(model, Mapping):
                continue
            nested = model.get("supporting_evidence")
            if isinstance(nested, Mapping) and isinstance(nested.get("appraisal"), Mapping):
                sources.append(nested["appraisal"])
            model_evidence = model.get("evidence")
            model_support = model_evidence.get("supporting_evidence") if isinstance(model_evidence, Mapping) else None
            if isinstance(model_support, Mapping) and isinstance(model_support.get("appraisal"), Mapping):
                sources.append(model_support["appraisal"])
            # The historical renderer's `appraisal` is a display model, not the
            # source evidence. Do not parse its formatted labels back into data.
    return sources


def _appraisal_date_label(appraisal: Mapping[str, Any], docs: list[Any]) -> str:
    label = _support_document_date(appraisal)
    if label:
        return label
    for item in docs:
        if isinstance(item, Mapping) and item.get("verified") and str(
            item.get("document_type") or item.get("type") or ""
        ).upper() == "INDIVIDUAL_APPRAISAL":
            label = _support_document_date(item)
            if label:
                return label
    return ""


def _log_owner_appraisal_analysis(
    property_code: str, resolver_status: Any, analysis: Mapping[str, Any] | None = None,
    *, error_type: str = "",
) -> None:
    """Emit a compact, non-PII appraisal diagnostic for any property code."""
    safe_property_code = str(property_code or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", safe_property_code):
        safe_property_code = "UNKNOWN"
    analysis = analysis if isinstance(analysis, Mapping) else {}
    area_basis = _text(analysis.get("area_basis")).upper()
    if area_basis not in {"USEFUL", "BUILT", "LAND"}:
        area_basis = "UNRESOLVED"
    uf_m2_source = _text(analysis.get("appraisal_uf_m2_source")).upper()
    if uf_m2_source not in {"EXPLICIT", "DERIVED_FROM_VERIFIED_VALUE_AND_SURFACE"}:
        uf_m2_source = "NONE"
    error_type = error_type or _text(analysis.get("extraction_error"))
    safe_error_type = error_type if re.fullmatch(r"[A-Za-z0-9_]{1,64}", error_type or "") else ""
    logger.info(
        "[OWNER_APPRAISAL_ANALYSIS] property_code=%s appraisal_resolver_status=%s "
        "appraisal_extraction_status=%s text_extracted=%s page_count=%s "
        "has_appraisal_value=%s has_surface_m2=%s area_basis=%s "
        "has_explicit_uf_m2=%s has_derived_uf_m2=%s uf_m2_source=%s error_type=%s",
        safe_property_code,
        _text(resolver_status).upper() or "UNKNOWN",
        _text(analysis.get("extraction_status")).upper() or "NOT_RUN",
        bool(analysis.get("text_extracted")),
        max(0, int(analysis.get("page_count") or 0)) if str(analysis.get("page_count") or "0").isdigit() else 0,
        _strict_appraisal_number(analysis.get("appraisal_value") or analysis.get("estimated_mid_uf")) is not None,
        (_strict_appraisal_number(analysis.get("surface_m2")) or 0) > 0,
        area_basis,
        uf_m2_source == "EXPLICIT",
        uf_m2_source == "DERIVED_FROM_VERIFIED_VALUE_AND_SURFACE",
        uf_m2_source,
        safe_error_type,
    )


def _appraisal_card(*, monthly: Mapping[str, Any], snapshot: Mapping[str, Any],
                    campaign_view: Mapping[str, Any], docs: list[Any], support_documents: list[dict[str, str]],
                    property_code: str, operation: str, current_price: Any,
                    recommended_price: Any,
                    extracted_appraisal: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Build a verified appraisal view; PDF existence alone never implies a value."""
    appraisal_docs = [item for item in support_documents if item.get("type") == "INDIVIDUAL_APPRAISAL"]
    # Multiple distinct verified appraisal documents for a property are ambiguous.
    urls = {item.get("url") for item in appraisal_docs if item.get("url")}
    if len(urls) > 1:
        return None
    document = appraisal_docs[0] if len(appraisal_docs) == 1 else None

    appraisal: Mapping[str, Any] | None = None
    for candidate in _appraisal_source_maps(monthly, snapshot, campaign_view, extracted_appraisal):
        code = _text(candidate.get("property_code") or candidate.get("codigo"))
        if code and code != property_code:
            continue
        if candidate.get("verified") is not True and candidate.get("source_verified") is not True:
            continue
        candidate_mid = candidate.get("estimated_mid_uf", candidate.get("appraisal_value"))
        if any(_strict_appraisal_number(candidate.get(key)) is not None for key in (
            "estimated_low_uf", "estimated_high_uf",
        )) or _strict_appraisal_number(candidate_mid) is not None:
            if candidate_mid is not None and candidate.get("estimated_mid_uf") is None:
                candidate = {**candidate, "estimated_mid_uf": candidate_mid}
            appraisal = candidate
            break

    if appraisal is None and document is None:
        return None

    is_rent = _text(operation).upper() in {"ARRIENDO", "RENT", "RENTAL"}
    title = "Tasación individual de tu propiedad"
    subtitle = "Referencia específica de valor para esta propiedad"
    method_copy = (
        "La estimación constituye una referencia comercial y no garantiza un canon final de arriendo."
        if is_rent else
        "La tasación constituye una referencia comercial basada en los antecedentes disponibles para la propiedad y no garantiza un precio final de venta."
    )
    document_url = document.get("url", "") if document else ""
    if appraisal is None:
        date_label = _appraisal_date_label({}, docs)
        document_metadata = (document.get("metadata") if document else "") or f"Propiedad {property_code} · PDF"
        if date_label and "Emitida " not in document_metadata:
            document_metadata += f" · Emitida {date_label}"
        # A PDF can explicitly state UF/m² and its surface basis without
        # exposing a central total appraisal value. Preserve that verified
        # number for the market-position bar while keeping the appraisal card
        # in DOCUMENT_ONLY mode. The resolver has already matched this PDF by
        # exact property code; the parser must also retain the source label.
        document_only_reference: dict[str, Any] = {}
        parsed_appraisal = extracted_appraisal if isinstance(extracted_appraisal, Mapping) else {}
        parsed_code = _text(parsed_appraisal.get("property_code"))
        parsed_source_file = _text(parsed_appraisal.get("source_file_id"))
        parsed_m2 = _strict_appraisal_number(parsed_appraisal.get("appraisal_uf_m2"))
        parsed_m2_source = _text(parsed_appraisal.get("appraisal_uf_m2_source")).upper()
        field_sources = parsed_appraisal.get("field_sources")
        has_m2_provenance = (
            isinstance(field_sources, Mapping)
            and bool(_text(field_sources.get("appraisal_uf_m2")))
            and parsed_m2_source in {"EXPLICIT", "DERIVED_FROM_VERIFIED_VALUE_AND_SURFACE"}
        )
        if parsed_code == property_code and parsed_source_file and parsed_m2 and has_m2_provenance:
            parsed_basis = _position_area_basis(
                parsed_appraisal.get("appraisal_uf_m2_unit") or parsed_appraisal.get("positioning_unit"),
                parsed_appraisal.get("area_basis") or parsed_appraisal.get("surface_basis"),
            )
            parsed_unit = _text(
                parsed_appraisal.get("appraisal_uf_m2_unit") or parsed_appraisal.get("positioning_unit")
            ) or (_position_basis_unit("UF", parsed_basis) if parsed_basis else "UF/m²")
            document_only_reference = {
                "property_code": property_code,
                "verified": True,
                "appraisal_uf_m2": parsed_m2,
                "appraisal_uf_m2_source": parsed_m2_source,
                "appraisal_uf_m2_unit": parsed_unit,
                "currency_basis": "UF",
                "area_basis": parsed_basis,
                "measurement_definition": "UF_PER_M2",
            }
        return {
            "mode": "DOCUMENT_ONLY", "title": title,
            "subtitle": "Tasación individual disponible",
            "issued_label": "", "source_date_label": date_label,
            "body_copy": "Esta propiedad cuenta con una tasación individual. Revisa el documento completo para consultar su valor de referencia y los antecedentes utilizados en su elaboración.",
            "metrics": [], "markers": [], "interpretation": "", "details": [],
            "method_copy": method_copy, "document_url": document_url,
            "document_title": "Tasación individual", "document_metadata": document_metadata,
            "property_code": property_code,
            **({"market_position_reference": document_only_reference} if document_only_reference else {}),
        }

    low = _strict_appraisal_number(appraisal.get("estimated_low_uf"))
    mid = _strict_appraisal_number(appraisal.get("estimated_mid_uf"))
    high = _strict_appraisal_number(appraisal.get("estimated_high_uf"))
    if low is not None and high is not None and low > high:
        low, high = None, None
    if mid is None and low is None and high is None:
        return None
    current = _strict_appraisal_number(current_price)
    recommended = _strict_appraisal_number(recommended_price)
    from .campaign import _format_client_price
    from analytics.owner_campaign_email_v2 import _appraisal_model as historical_appraisal_model
    historical_model = historical_appraisal_model({"appraisal": appraisal}, current, operation)

    def price_label(amount: float | None) -> str:
        return _format_client_price(amount, operation) if amount is not None else ""

    gap_pct = ((current / mid) - 1) * 100 if current is not None and mid is not None else None
    gap_amount = current - mid if current is not None and mid is not None else None
    stored_position = ""
    if current is not None and low is not None and high is not None:
        stored_position = "ABOVE_RANGE" if current > high else "BELOW_RANGE" if current < low else "WITHIN_RANGE"
    conflict = False

    metrics: list[dict[str, str]] = []
    if mid is not None:
        metrics.append({"label": "Valor de referencia", "value": historical_model.get("mid_label") or price_label(mid)})
    if low is not None and high is not None:
        metrics.append({"label": "Rango estimado", "value": historical_model.get("range_label") or f"{price_label(low)} – {price_label(high)}"})
    if current is not None:
        metrics.append({"label": "Precio publicado", "value": historical_model.get("current_label") or price_label(current)})
    if gap_pct is not None:
        metrics.append({
            "label": "Diferencia frente a la tasación",
            "value": historical_model.get("gap_label") or f"{gap_pct:+.1f}%".replace(".", ","),
            "note": ((historical_model.get("gap_amount_label") or (("+" if gap_amount > 0 else "−" if gap_amount < 0 else "") + price_label(abs(gap_amount)))) + " frente al valor de referencia") if gap_amount is not None else "",
        })
    interpretation = ""
    if current is not None:
        if low is not None and high is not None:
            if current > high:
                pct = f"{abs(gap_pct):.1f}%".replace(".", ",") if gap_pct is not None else ""
                interpretation = "El precio publicado se encuentra por encima del rango de referencia de la tasación." + (f" Frente al valor central estimado, la diferencia es aproximadamente {pct}." if pct else "")
            elif current < low:
                interpretation = "El precio publicado se encuentra por debajo del rango de referencia de la tasación."
            else:
                interpretation = "El precio publicado se encuentra dentro del rango estimado por la tasación."
        elif mid is not None and gap_pct is not None:
            direction = "sobre" if gap_pct > 0 else "bajo" if gap_pct < 0 else "en línea con"
            if abs(gap_pct) <= 0.05:
                interpretation = "El precio publicado se encuentra en línea con el valor de referencia de la tasación."
            else:
                interpretation = f"El precio publicado se encuentra {abs(gap_pct):.1f}% {direction} el valor de referencia de la tasación.".replace(".", ",", 1)
    date_label = _appraisal_date_label(appraisal, docs)
    details = []
    for label, value in (("Valor central", mid), ("Rango inferior", low),
                         ("Rango superior", high), ("UF/m² tasado", _strict_appraisal_number(appraisal.get("appraisal_uf_m2"))),
                         ("Precio recomendado PROCASA", recommended)):
        if value is None:
            continue
        detail_value = _communal_market_value(value, suffix=" UF/m²") if label == "UF/m² tasado" else price_label(value)
        details.append({"label": label, "value": detail_value})
    if recommended is not None and mid is not None:
        recommended_gap = (recommended / mid - 1) * 100
        direction = "sobre" if recommended_gap > 0.05 else "bajo" if recommended_gap < -0.05 else "en línea con"
        recommended_position = "en línea con la tasación" if direction == "en línea con" else f"{abs(recommended_gap):.1f}% {direction} la tasación".replace(".", ",", 1)
        details.append({"label": "Posición del precio recomendado frente a la tasación", "value": recommended_position})
    if stored_position:
        details.append({
            "label": "Posición del precio publicado frente al rango",
            "value": {"ABOVE_RANGE": "Por encima del rango", "WITHIN_RANGE": "Dentro del rango", "BELOW_RANGE": "Por debajo del rango"}[stored_position],
        })
    if date_label:
        details.append({"label": "Fecha de tasación", "value": date_label})
    methodology = _text(appraisal.get("appraisal_method") or appraisal.get("methodology") or appraisal.get("method_label") or appraisal.get("method"))
    if methodology:
        details.append({"label": "Metodología", "value": methodology})
    document_metadata = (document.get("metadata") if document else "") or f"Propiedad {property_code} · PDF"
    if date_label and "Emitida " not in document_metadata:
        document_metadata += f" · Emitida {date_label}"
    return {
        "mode": "STRUCTURED", "title": title, "subtitle": subtitle,
        "issued_label": "",
        "metrics": metrics[:4], "markers": [], "range_band": None,
        "interpretation": interpretation,
        "conflict": conflict, "details": details,
        "method_copy": method_copy, "document_url": document_url,
        "document_title": "Tasación individual", "document_metadata": document_metadata,
        "property_code": property_code,
        "market_position_reference": {
            "property_code": property_code,
            "verified": True,
            "appraisal_value_uf": mid,
            "currency_basis": "UF" if mid is not None else "",
            "appraisal_uf_m2": _strict_appraisal_number(appraisal.get("appraisal_uf_m2")),
            "appraisal_uf_m2_source": _text(appraisal.get("appraisal_uf_m2_source")),
            "measurement_definition": (
                "UF_PER_M2" if _strict_appraisal_number(appraisal.get("appraisal_uf_m2")) is not None else ""
            ),
            "source_date_label": date_label,
            "area_basis": _position_area_basis(
                appraisal.get("appraisal_uf_m2_unit") or appraisal.get("positioning_unit")
                or appraisal.get("reference_unit") or appraisal.get("unit"),
                appraisal.get("area_basis") or appraisal.get("surface_basis") or appraisal.get("surface_ref_basis"),
            ),
            "surface_m2": _strict_appraisal_number(
                appraisal.get("surface_m2") or appraisal.get("appraisal_surface_m2")
                or appraisal.get("surface_ref_m2")
            ),
            "appraisal_uf_m2_unit": _text(
                appraisal.get("appraisal_uf_m2_unit") or appraisal.get("positioning_unit")
                or appraisal.get("reference_unit") or appraisal.get("unit")
            ),
        },
    }


class _CampaignHtmlEvidence(HTMLParser):
    """Extract only visible text from explicitly allowlisted email classes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, list[str] | None]] = []
        self.captures: dict[str, list[list[str]]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set(dict(attrs).get("class", "").split())
        capture: list[str] | None = None
        for class_name in classes & _TARGET_CLASSES:
            capture = []
            self.captures.setdefault(class_name, []).append(capture)
        self.stack.append((tag, capture))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        for _tag, capture in self.stack:
            if capture is not None:
                capture.append(data)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            stack_tag, _capture = self.stack[index]
            if stack_tag == tag:
                del self.stack[index:]
                return


def extract_verified_email_evidence(email_html: str | None) -> dict[str, Any]:
    """Read selected historical text from the immutable sent HTML artifact."""
    if not isinstance(email_html, str) or not email_html.strip():
        return {}
    parser = _CampaignHtmlEvidence()
    try:
        parser.feed(email_html)
        parser.close()
    except Exception:
        return {}

    result: dict[str, Any] = {}
    for key, captures in parser.captures.items():
        values = [" ".join("".join(parts).split()) for parts in captures]
        result[key] = [value for value in values if value]
    activity = result.get("activity-stat-value-single", [])
    if activity:
        lead_value: Any = activity[0] if len(activity) > 0 else None
        lead_number = _number_from_label(lead_value)
        if lead_number is not None and re.search(r"\bleads?\b", _text(lead_value), flags=re.IGNORECASE):
            lead_value = int(lead_number) if lead_number.is_integer() else lead_number
        result["activity_90d"] = {
            "leads": lead_value,
            "conversations": activity[1] if len(activity) > 1 else None,
            "visits": activity[2] if len(activity) > 2 else None,
            "summary": (result.get("activity-copy-single") or [None])[0],
        }
    heading = (result.get("single-heading") or [None])[0]
    if heading:
        parts = [part.strip() for part in re.split(r"\s*[·|]\s*", heading, maxsplit=1)]
        if parts:
            result["property_type"] = parts[0]
        if len(parts) > 1:
            result["commune"] = parts[1]
    operation_lines = result.get("single-operation", [])
    if operation_lines:
        # Feature text in the immutable sent email is a verified historical
        # source for the denominator used by that email's UF/m² comparison.
        surface_pattern = re.compile(
            r"(?:m²|m2)\s*([0-9][0-9.,]*)\s*m²\s*(útil(?:es)?|construid[oa]s?|terreno)",
            flags=re.IGNORECASE,
        )
        surfaces: dict[str, float] = {}
        for line in operation_lines:
            for match in surface_pattern.finditer(line):
                value = _number_from_label(match.group(1))
                kind = match.group(2).casefold()
                key = "useful" if kind.startswith("útil") else "built" if kind.startswith("constru") else "land"
                if value is not None and value > 0:
                    surfaces.setdefault(key, value)
        if surfaces:
            result["verified_property_surfaces_m2"] = surfaces
    labels = result.get("macro-kpi-label-single", [])
    values = result.get("macro-kpi-value-single", [])
    notes = result.get("macro-kpi-note-single", [])
    if labels or values or notes or result.get("macro-copy-single"):
        result["market_context"] = {
            "reference_month": (result.get("macro-heading-single") or [None])[0],
            "kpis": [
                {"label": label, "value": values[i] if i < len(values) else None,
                 "note": notes[i] if i < len(notes) else None}
                for i, label in enumerate(labels)
            ],
            "summary": " ".join(result.get("macro-highlight-single", []) + result.get("macro-copy-single", [])),
            "sources": (result.get("macro-source-single") or [None])[0],
        }
    return result


def _activity_datetime(value: Any) -> datetime | None:
    """Parse a source timestamp without substituting the current time."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _activity_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    number = _number_from_label(value)
    if number is None or number < 0 or not float(number).is_integer():
        return None
    return int(number)


def _owner_activity_summary(value: Any) -> str:
    summary = _text(value)
    forbidden = (
        "CANONICAL_READ_ONLY_RECONSTRUCTION", "MONTHLY_SNAPSHOT",
        "FROZEN_CAMPAIGN_EVIDENCE", "FROZEN_SENT_RENDER_MODEL",
        "SOURCE_ERROR", "INTEGRITY_FAILURE",
    )
    if any(term.casefold() in summary.casefold() for term in forbidden):
        return ""
    return summary


def _complete_activity_candidate(value: Any, *, source: str, cutoff: Any = None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    state = _text(value.get("state")).upper()
    if state in {"UNKNOWN", "SOURCE_ERROR", "INTEGRITY_FAILURE", "UNRESOLVED"}:
        return None
    leads = _activity_count(value.get("leads", value.get("total_leads", value.get("lead_total"))))
    conversations = _activity_count(value.get("conversations", value.get("conversation_count")))
    visits = _activity_count(value.get("visits", value.get("visit_count")))
    if leads is None or conversations is None or visits is None:
        return None
    window_end = _activity_datetime(
        value.get("window_end") or value.get("cutoff_at") or value.get("as_of")
        or value.get("source_date") or cutoff
    )
    return {
        "state": "VERIFIED", "leads": leads, "conversations": conversations,
        "visits": visits, "source": source, "window_end": window_end,
        "summary": _text(value.get("summary")),
    }


def _reconstruct_owner_activity_90d(
    db: Any, property_code: str, *, window_end: datetime, operation: str,
) -> dict[str, Any]:
    """Rebuild historical activity using the campaign's established semantics.

    Leads are unique by normalized phone (or lead id when no phone exists),
    conversations by conversation_id rather than message count, and visits
    only by a signed visit with a verifiable accepted timestamp.
    """
    cutoff = window_end.astimezone(timezone.utc)
    window_start = cutoff - timedelta(days=90)
    code_values: list[Any] = [property_code]
    try:
        if property_code.isdigit():
            code_values.append(int(property_code))
    except Exception:
        pass
    try:
        raw_leads = list(db["leads"].find(
            {"prospecto.codigo": {"$in": code_values}},
            {"_id": 1, "prospecto.codigo": 1, "prospecto.operacion": 1,
             "created_at": 1, "conversation_id": 1, "source": 1, "origen": 1,
             "portal": 1, "test_mode": 1, "is_duplicate": 1, "duplicate": 1,
             "duplicate_of": 1, "phone": 1, "stage_history": 1},
        ))
        eligible_leads: dict[str, Mapping[str, Any]] = {}
        lead_identity: set[str] = set()
        offer_leads: set[str] = set()
        closing_leads: set[str] = set()
        undated_lead_candidates = 0
        for lead in raw_leads:
            prospect = lead.get("prospecto") if isinstance(lead.get("prospecto"), Mapping) else {}
            if str(prospect.get("codigo") or "").strip() != property_code:
                continue
            source_text = " ".join(str(lead.get(key) or "") for key in ("source", "origen", "portal")).casefold()
            if lead.get("test_mode") is True or any(flag in source_text for flag in ("test", "qa", "internal")):
                continue
            if lead.get("is_duplicate") is True or lead.get("duplicate") is True or lead.get("duplicate_of") not in (None, "", False):
                continue
            lead_operation = _text(prospect.get("operacion")).casefold()
            if operation == "VENTA" and any(word in lead_operation for word in ("arriendo", "arriend", "rent")):
                continue
            if operation == "ARRIENDO" and any(word in lead_operation for word in ("venta", "sell")):
                continue
            created = _activity_datetime(lead.get("created_at"))
            if created is None:
                undated_lead_candidates += 1
                continue
            if not (window_start <= created <= cutoff):
                continue
            phone = "".join(char for char in str(lead.get("phone") or "") if char.isdigit())
            if phone.startswith("56") and len(phone) == 11:
                phone = phone[2:]
            identity = f"phone:{phone}" if phone else f"lead:{lead.get('_id')}"
            if identity in lead_identity:
                continue
            lead_identity.add(identity)
            lead_id = str(lead.get("_id"))
            eligible_leads[lead_id] = lead
            for stage_event in lead.get("stage_history") or []:
                if not isinstance(stage_event, Mapping):
                    continue
                reached = str(stage_event.get("to") or "").strip().upper()
                event_at = _activity_datetime(stage_event.get("timestamp"))
                if event_at is None or not (created <= event_at <= cutoff):
                    continue
                if reached in {"OFFER", "NEGOTIATION", "CLOSED_WON"}:
                    offer_leads.add(lead_id)
                if reached == "CLOSED_WON":
                    closing_leads.add(lead_id)

        native_ids = list(eligible_leads.keys())
        event_or: list[dict[str, Any]] = [{"property_code": {"$in": code_values}}]
        if native_ids:
            event_or.append({"lead_id": {"$in": native_ids}})
        raw_events = list(db["conversation_events"].find(
            {"$or": event_or},
            {"property_code": 1, "lead_id": 1, "conversation_id": 1,
             "timestamp": 1, "created_at": 1, "event_type": 1,
             "actor_type": 1, "test_mode": 1, "source": 1},
        ))
        conversations: set[str] = set()
        for event in raw_events:
            if event.get("test_mode") is True or str(event.get("source") or "").casefold() in {"owner_campaign_test", "qa", "internal"}:
                continue
            linked = str(event.get("property_code") or "").strip()
            if not linked and event.get("lead_id") is not None:
                linked_lead = eligible_leads.get(str(event.get("lead_id")))
                if linked_lead:
                    prospect = linked_lead.get("prospecto") if isinstance(linked_lead.get("prospecto"), Mapping) else {}
                    linked = str(prospect.get("codigo") or "").strip()
            if linked != property_code:
                continue
            event_type = str(event.get("event_type") or "").casefold()
            actor = str(event.get("actor_type") or "").casefold()
            if event_type not in {"customer_message_received", "human_message_sent"} and actor not in {"customer", "owner"}:
                continue
            occurred = _activity_datetime(event.get("timestamp") or event.get("created_at"))
            if occurred is None or not (window_start <= occurred <= cutoff):
                continue
            conversation_id = str(event.get("conversation_id") or "").strip()
            if conversation_id:
                conversations.add(conversation_id)

        raw_visits: list[Mapping[str, Any]] = []
        for collection_name in ("visitas", "ordenes_visitas"):
            try:
                raw_visits.extend(list(db[collection_name].find(
                    {"property_code": {"$in": code_values}},
                    {"property_code": 1, "status": 1, "timeline": 1,
                     "visita_code": 1, "order_id": 1, "_id": 1},
                )))
            except Exception:
                raise
        visits: set[str] = set()
        for visit in raw_visits:
            if str(visit.get("property_code") or "").strip() != property_code or str(visit.get("status") or "").casefold() != "signed":
                continue
            for event in visit.get("timeline") or []:
                if not isinstance(event, Mapping) or str(event.get("action") or "").casefold() != "accepted":
                    continue
                accepted_at = _activity_datetime(event.get("server_timestamp"))
                if accepted_at is not None and window_start <= accepted_at <= cutoff:
                    visits.add(str(visit.get("visita_code") or visit.get("order_id") or visit.get("_id") or ""))
                break
        leads_value = len(lead_identity)
        # An exact-property lead without a timestamp cannot be assigned to or
        # excluded from this historical window. Do not turn that uncertainty
        # into a verified zero when there are no dated leads to count.
        unresolved_leads = undated_lead_candidates > 0 and leads_value == 0
        return {
            "state": "INTEGRITY_FAILURE" if unresolved_leads else "VERIFIED",
            "leads": None if unresolved_leads else leads_value,
            "conversations": len(conversations), "visits": len(visits),
            "offers": len(offer_leads), "closings": len(closing_leads),
            "offers_source": "leads.stage_history:OFFER|NEGOTIATION|CLOSED_WON",
            "closings_source": "leads.stage_history:CLOSED_WON",
            "source": "CANONICAL_READ_ONLY_RECONSTRUCTION",
            "window_start": window_start, "window_end": cutoff,
            "summary": "",
            "integrity": "UNVERIFIED_LEAD_TIMESTAMP" if unresolved_leads else (
                "PARTIAL_UNDATED_LEAD_CANDIDATES" if undated_lead_candidates else "OK"
            ),
            "undated_lead_candidates": undated_lead_candidates,
        }
    except Exception:
        return {
            "state": "SOURCE_ERROR", "leads": None, "conversations": None,
            "visits": None, "offers": None, "closings": None,
            "offers_source": "NOT_INSTRUMENTED", "closings_source": "NOT_INSTRUMENTED",
            "source": "CANONICAL_READ_ONLY_RECONSTRUCTION",
            "window_start": window_start, "window_end": cutoff,
            "summary": "", "integrity": "SOURCE_ERROR",
        }


def resolve_owner_activity_90d(
    db: Any, property_code: str, *, candidates: tuple[Any, ...],
    window_end_candidates: tuple[Any, ...], operation: str,
) -> dict[str, Any]:
    """Resolve one canonical activity result for both KPI and funnel."""
    for index, candidate in enumerate(candidates):
        source = "MONTHLY_SNAPSHOT" if index == 0 else "FROZEN_SENT_RENDER_MODEL"
        resolved = _complete_activity_candidate(candidate, source=source)
        if resolved is not None:
            end = resolved.get("window_end")
            resolved["window_start"] = end - timedelta(days=90) if end else None
            resolved["integrity"] = "OK"
            return resolved
    cutoff = next((_activity_datetime(value) for value in window_end_candidates if _activity_datetime(value)), None)
    if cutoff is None:
        return {
            "state": "INTEGRITY_FAILURE", "leads": None, "conversations": None,
            "visits": None, "source": "UNRESOLVED_CUTOFF", "window_start": None,
            "window_end": None, "summary": "", "integrity": "UNRESOLVED_CUTOFF",
        }
    return _reconstruct_owner_activity_90d(db, property_code, window_end=cutoff, operation=operation)


def resolve_owner_commercial_funnel_90d(
    db: Any, property_code: str, *, candidates: tuple[Any, ...],
    window_end_candidates: tuple[Any, ...], operation: str,
) -> dict[str, Any]:
    """Resolve the four owner-facing commercial stages against the report cutoff.

    Complete, frozen four-stage evidence wins. Legacy three-stage activity data
    is intentionally not promoted into a complete funnel; it falls back to
    exact-property canonical lead/visit records and timestamped stage history.
    """
    normalized_stages = ("LEADS", "VISITS", "OFFERS", "CLOSINGS")
    legacy_activity: dict[str, Any] | None = None
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            continue
        legacy = _complete_activity_candidate(
            candidate,
            source="MONTHLY_SNAPSHOT" if index == 0 else "FROZEN_CAMPAIGN_EVIDENCE",
        )
        if legacy is not None and legacy_activity is None:
            legacy_activity = legacy
        funnel = candidate.get("commercial_funnel_90d") or candidate.get("funnel")
        if not isinstance(funnel, Mapping):
            continue
        values = funnel.get("stages") if isinstance(funnel.get("stages"), Mapping) else funnel
        resolved: dict[str, Any] = {}
        complete = True
        for stage in normalized_stages:
            item = values.get(stage.lower()) or values.get(stage) if isinstance(values, Mapping) else None
            item = item if isinstance(item, Mapping) else {"count": item}
            count = _activity_count(item.get("count", item.get("value")))
            status = _text(item.get("status") or funnel.get("status")).upper()
            if count is None or status in {"SOURCE_ERROR", "INTEGRITY_ERROR", "IDENTITY_UNRESOLVED", "UNKNOWN"}:
                complete = False
                break
            resolved[stage] = {"count": count, "status": "VERIFIED", "source": _text(item.get("source") or funnel.get("source"))}
        if complete:
            cutoff = next((_activity_datetime(value) for value in (
                funnel.get("cutoff"), funnel.get("window_end"), candidate.get("cutoff_at"),
                candidate.get("window_end"),
            ) if _activity_datetime(value)), None)
            if cutoff:
                start = cutoff - timedelta(days=90)
                return _build_owner_funnel(resolved, source="MONTHLY_SNAPSHOT" if index == 0 else "FROZEN_CAMPAIGN_EVIDENCE", cutoff=cutoff, start=start)

    cutoff = next((_activity_datetime(value) for value in window_end_candidates if _activity_datetime(value)), None)
    if cutoff is None and legacy_activity is not None:
        cutoff = legacy_activity.get("window_end")
    if cutoff is None:
        return _unavailable_owner_funnel("UNRESOLVED_CUTOFF", "IDENTITY_UNRESOLVED")
    reconstructed = _reconstruct_owner_activity_90d(
        db, property_code, window_end=cutoff, operation=operation,
    )
    if reconstructed.get("state") == "SOURCE_ERROR" and legacy_activity is None:
        return _unavailable_owner_funnel(reconstructed.get("integrity") or "SOURCE_ERROR", "SOURCE_ERROR", cutoff=cutoff)
    rows = {
        "LEADS": {"count": reconstructed.get("leads"), "status": "VERIFIED" if reconstructed.get("leads") is not None else "INTEGRITY_ERROR", "source": "leads.created_at"},
        "VISITS": {"count": reconstructed.get("visits"), "status": "VERIFIED" if reconstructed.get("visits") is not None else "INTEGRITY_ERROR", "source": "visitas.status+timeline.accepted"},
        "OFFERS": {"count": reconstructed.get("offers"), "status": "VERIFIED" if reconstructed.get("offers") is not None else "NOT_INSTRUMENTED", "source": reconstructed.get("offers_source") or "NOT_INSTRUMENTED"},
        "CLOSINGS": {"count": reconstructed.get("closings"), "status": "VERIFIED" if reconstructed.get("closings") is not None else "NOT_INSTRUMENTED", "source": reconstructed.get("closings_source") or "NOT_INSTRUMENTED"},
    }
    if legacy_activity is not None:
        # Preserve frozen leads/visits as higher-priority stage evidence. The
        # old conversations field remains activity metadata, never a funnel stage.
        for key, legacy_key in (("LEADS", "leads"), ("VISITS", "visits")):
            value = _activity_count(legacy_activity.get(legacy_key))
            if value is not None:
                rows[key] = {"count": value, "status": "VERIFIED", "source": legacy_activity.get("source")}
    result = _build_owner_funnel(rows, source=reconstructed.get("source") or "CANONICAL_READ_ONLY_RECONSTRUCTION", cutoff=cutoff, start=cutoff - timedelta(days=90))
    result.update({
        "conversations": legacy_activity.get("conversations") if legacy_activity else reconstructed.get("conversations"),
        "summary": legacy_activity.get("summary", "") if legacy_activity else reconstructed.get("summary", ""),
        "integrity": reconstructed.get("integrity", "OK"),
    })
    return result


def _unavailable_owner_funnel(reason: str, status: str, *, cutoff: datetime | None = None) -> dict[str, Any]:
    rows = {stage: {"count": None, "status": status, "source": "UNAVAILABLE"} for stage in ("LEADS", "VISITS", "OFFERS", "CLOSINGS")}
    return _build_owner_funnel(rows, source=reason, cutoff=cutoff, start=cutoff - timedelta(days=90) if cutoff else None)


def _build_owner_funnel(rows: Mapping[str, Mapping[str, Any]], *, source: str, cutoff: datetime | None, start: datetime | None) -> dict[str, Any]:
    ordered = ("LEADS", "VISITS", "OFFERS", "CLOSINGS")
    labels = {"LEADS": "Leads", "VISITS": "Visitas", "OFFERS": "Ofertas", "CLOSINGS": "Cierre"}
    previous_count: int | None = None
    stages: list[dict[str, Any]] = []
    base_count = _activity_count((rows.get("LEADS") or {}).get("count"))
    shape_widths = (100.0, 76.0, 54.0, 34.0)
    for index, key in enumerate(ordered):
        item = rows.get(key) or {}
        count = _activity_count(item.get("count"))
        stage_status = _text(item.get("status") or ("VERIFIED" if count is not None else "SOURCE_ERROR")).upper()
        if stage_status == "NOT_INSTRUMENTED":
            count = None
        if count is None:
            ratio = "Sin información registrada" if stage_status == "NOT_INSTRUMENTED" else "Dato no disponible"
        elif index == 0:
            ratio = "100%" if count > 0 else "Sin base de comparación"
        else:
            pct = (count / previous_count * 100) if previous_count is not None and previous_count > 0 else None
            if pct is None:
                ratio = "Sin base de comparación"
            elif key == "VISITS" and count == 0 and previous_count > 0:
                ratio = "0% de leads"
            else:
                ratio = f"{pct:.1f}% de {labels[ordered[index - 1]].lower()}".replace(".", ",")
        fill_width = (count / base_count * 100) if count is not None and base_count and base_count > 0 else 0.0
        stages.append({"key": key, "label": labels[key], "count": count,
                       "value": str(count) if count is not None else "—", "ratio": ratio,
                       "width": shape_widths[index], "shape_width": shape_widths[index],
                       "fill_width": round(max(0.0, min(100.0, fill_width)), 1), "status": stage_status,
                       "confidence": "VERIFIED" if count is not None else "UNAVAILABLE",
                       "source": _text(item.get("source") or source)})
        previous_count = count
    return {
        "available": True, "stages": stages, "source": source,
        "cutoff": cutoff, "start_date": start,
        "source_status": "VERIFIED" if all(item["status"] == "VERIFIED" for item in stages) else "PARTIAL",
    }


def _activity_funnel(activity: Mapping[str, Any]) -> dict[str, Any]:
    """Prepare client-readable funnel stages without division by zero."""
    leads, conversations, visits = (
        _activity_count(activity.get(key)) for key in ("leads", "conversations", "visits")
    )
    valid = all(value is not None for value in (leads, conversations, visits))
    if not valid:
        return {"available": False, "stages": []}

    def pct(numerator: int, denominator: int) -> str | None:
        if denominator <= 0:
            return None
        return f"{numerator / denominator * 100:.1f}".replace(".", ",") + "%"

    def width(value: int) -> float:
        if leads <= 0:
            return 0.0
        return max(0.0, min(100.0, value / leads * 100))

    lead_note = "100%" if leads > 0 else "Sin base de comparación"
    conversation_pct = pct(conversations, leads)
    conversation_note = f"{conversation_pct} de los leads" if conversation_pct is not None else "Sin base de comparación"
    visit_denominator = conversations if conversations > 0 else leads
    visit_pct = pct(visits, visit_denominator)
    visit_basis = "de las conversaciones" if conversations > 0 else "de los leads"
    visit_note = f"{visit_pct} {visit_basis}" if visit_pct is not None else "Sin base de comparación"
    return {
        "available": True,
        "stages": [
            {"label": "Consultas recibidas", "value": leads, "ratio": lead_note, "width": width(leads)},
            {"label": "Conversaciones", "value": conversations, "ratio": conversation_note, "width": width(conversations)},
            {"label": "Visitas coordinadas", "value": visits, "ratio": visit_note, "width": width(visits)},
        ],
    }


def _as_period(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    text = str(value or "").strip()
    return text[:7] if re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", text) else ""


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _source_date_label(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    raw = _text(value)
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else raw


def _select_monthly_snapshot(record: Mapping[str, Any] | None, property_code: str) -> Mapping[str, Any] | None:
    if not isinstance(record, Mapping) or str(record.get("property_code") or "") != property_code:
        return None
    current = record.get("current_portal_state")
    if isinstance(current, Mapping) and _as_period(current.get("period") or current.get("generated_at")):
        return current
    snapshots = record.get("monthly_snapshots")
    if not isinstance(snapshots, list):
        return None
    valid = [
        item for item in snapshots
        if isinstance(item, Mapping)
        and str(item.get("property_code") or property_code) == property_code
        and _as_period(item.get("period"))
    ]
    return max(valid, key=lambda item: _as_period(item.get("period")), default=None)


def persist_monthly_snapshot(
    db: Any,
    *,
    property_code: Any,
    owner_email: Any,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one immutable monthly snapshot and advance the current pointer.

    This internal writer is intentionally not called by page requests. A trusted
    monthly preparation process may call it after validating its source data.
    Repeating an identical period is a no-op; conflicting same-period data is
    rejected instead of silently rewriting history.
    """
    code = str(property_code or "").strip()
    period = _as_period(snapshot.get("period")) if isinstance(snapshot, Mapping) else ""
    if not code or not period or not isinstance(snapshot, Mapping):
        raise ValueError("monthly_snapshot_identity_or_period_missing")
    if _contains_private_access_field(snapshot):
        raise ValueError("monthly_snapshot_contains_private_access_data")
    snapshot_value = dict(snapshot)
    snapshot_value["property_code"] = code
    key = owner_property_portal_id(code, owner_email)
    collection = db[OWNER_PROPERTY_PORTAL_COLLECTION]
    existing = collection.find_one({"_id": key, "property_code": code, "owner_key": key})
    existing_snapshots = existing.get("monthly_snapshots", []) if isinstance(existing, Mapping) else []
    existing_snapshots = existing_snapshots if isinstance(existing_snapshots, list) else []
    same_period = next((item for item in existing_snapshots if isinstance(item, Mapping) and _as_period(item.get("period")) == period), None)
    if same_period is not None:
        if _snapshot_values_equal(dict(same_period), snapshot_value):
            return {"status": "UNCHANGED", "period": period, "current_period": _as_period((existing.get("current_portal_state") or {}).get("period"))}
        raise ValueError("monthly_snapshot_period_is_immutable")

    current = existing.get("current_portal_state") if isinstance(existing, Mapping) else None
    current_period = _as_period(current.get("period")) if isinstance(current, Mapping) else ""
    update: dict[str, Any] = {
        "$setOnInsert": {
            "property_code": code,
            "owner_key": key,
            "schema_version": MONTHLY_SNAPSHOT_SCHEMA_VERSION,
        },
        "$push": {"monthly_snapshots": {"$each": [snapshot_value], "$sort": {"period": 1}}},
        "$set": {"updated_at": snapshot_value.get("generated_at") or datetime.now(timezone.utc)},
    }
    if not current_period or period >= current_period:
        update["$set"]["current_portal_state"] = snapshot_value
    try:
        result = collection.update_one(
            {"_id": key, "monthly_snapshots.period": {"$ne": period}},
            update,
            upsert=True,
        )
    except Exception:
        # Handle the concurrent same-period insert case without accepting
        # conflicting data or hiding unrelated database failures.
        raced = collection.find_one({"_id": key, "property_code": code, "owner_key": key})
        raced_snapshots = raced.get("monthly_snapshots", []) if isinstance(raced, Mapping) else []
        raced_period = next((item for item in raced_snapshots if isinstance(item, Mapping) and _as_period(item.get("period")) == period), None)
        if raced_period is not None and _snapshot_values_equal(dict(raced_period), snapshot_value):
            current_state = raced.get("current_portal_state") or {}
            return {"status": "UNCHANGED", "period": period, "current_period": _as_period(current_state.get("period"))}
        raise
    if result.modified_count == 0 and result.upserted_id is None:
        raced = collection.find_one({"_id": key, "property_code": code, "owner_key": key}) or {}
        raced_snapshots = raced.get("monthly_snapshots", [])
        raced_period = next((item for item in raced_snapshots if isinstance(item, Mapping) and _as_period(item.get("period")) == period), None)
        if raced_period is not None and _snapshot_values_equal(dict(raced_period), snapshot_value):
            return {"status": "UNCHANGED", "period": period, "current_period": _as_period((raced.get("current_portal_state") or {}).get("period"))}
        raise ValueError("monthly_snapshot_period_is_immutable")
    latest = collection.find_one({"_id": key}, {"_id": 0, "current_portal_state.period": 1}) or {}
    latest_state = latest.get("current_portal_state") or {}
    return {"status": "CREATED", "period": period, "current_period": _as_period(latest_state.get("period"))}


def _value(primary: Mapping[str, Any], fallback: Mapping[str, Any], key: str) -> Any:
    value = primary.get(key)
    return value if value is not None else fallback.get(key)


def _snapshot_values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) and isinstance(right, datetime):
        return _as_utc(left) == _as_utc(right)
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_snapshot_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_snapshot_values_equal(a, b) for a, b in zip(left, right))
    return left == right


def _contains_private_access_field(value: Any) -> bool:
    blocked = {"token", "token_hash", "owner_email", "private_url"}
    if isinstance(value, Mapping):
        return any(str(key).casefold() in blocked or _contains_private_access_field(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_private_access_field(item) for item in value)
    return False


def _text(value: Any) -> str:
    return html.unescape(str(value if value is not None else "")).strip()


def _communal_market_value(value: Any, *, suffix: str = "") -> str:
    """Format verified numeric market data without turning missing values into zero."""
    number = _number_from_label(value)
    if number is None:
        return ""
    if suffix in {"publications", "score"}:
        rendered = f"{int(number):,}".replace(",", ".")
    elif suffix == "range" and number.is_integer():
        rendered = f"{int(number):,}".replace(",", ".")
    elif suffix in {"rental-m2", "precise-percent"}:
        rendered = f"{number:,.2f}".replace(",", "\0").replace(".", ",").replace("\0", ".")
    else:
        rendered = f"{number:,.1f}".replace(",", "\0").replace(".", ",").replace("\0", ".")
    return f"{rendered}{suffix if suffix not in {'publications', 'range', 'score', 'rental-m2', 'precise-percent'} else ''}"


def _communal_market_label(value: Any, mapping: Mapping[str, str]) -> str:
    return mapping.get(_text(value).casefold(), "")


def _communal_market_title(property_type: str, commune: str) -> str:
    normalized = _text(property_type)
    plural = {
        "departamento": "departamentos", "casa": "casas", "parcela": "parcelas",
        "terreno": "terrenos", "oficina": "oficinas", "local comercial": "locales comerciales",
    }.get(normalized.casefold(), normalized.casefold())
    if not plural or not commune:
        return "Contexto del mercado comunal"
    return f"Mercado de {plural} en {commune}"


def _communal_market_card(
    db: Any, *, commune: str, property_type: str, operation: str, document_url: str,
) -> dict[str, Any] | None:
    """Build a read-only context card from the exact communal/type/operation record."""
    operation_key = _text(operation).upper()
    market_field = {"VENTA": "mercado_venta", "ARRIENDO": "mercado_arriendo"}.get(operation_key)
    if not commune or not property_type or not market_field:
        return None
    match_key = _communal_match_key(commune, property_type)
    if not match_key:
        return None
    projection = {
        "_id": 0, "match_key": 1, "comuna": 1, "tipo_propiedad": 1, market_field: 1,
        "mercado_arriendo": 1, "indicadores_mercado": 1,
        "rangos_precio_venta": 1, "rangos_precio_arriendo": 1, "source": 1,
        "currency_basis": 1, "reference_unit": 1, "area_basis": 1,
        "reference_area_basis": 1, "surface_basis": 1,
    }
    try:
        # Match the dataset's normalized key exactly. Limiting to two makes
        # duplicate keys fail closed instead of selecting a report arbitrarily.
        matches = list(db["mercado_comunal"].find(
            {"match_key": match_key}, projection,
        ).limit(2))
    except Exception:
        return None
    if len(matches) != 1:
        return None
    record = matches[0]
    if (
        record.get("match_key") != match_key
        or _communal_match_key(record.get("comuna"), record.get("tipo_propiedad")) != match_key
    ):
        return None
    operation_data = record.get(market_field)
    if not isinstance(operation_data, Mapping):
        return None
    indicators = record.get("indicadores_mercado") if isinstance(record.get("indicadores_mercado"), Mapping) else {}
    ranges = record.get("rangos_precio_venta" if operation_key == "VENTA" else "rangos_precio_arriendo")
    ranges = ranges if isinstance(ranges, Mapping) else {}
    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
    source_date_value = source.get("fecha_reporte")
    report_date = ""
    if isinstance(source_date_value, (datetime, date)):
        report_date = source_date_value.strftime("%d/%m/%Y")
    else:
        source_date_raw = _text(source_date_value)
        for date_format in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                report_date = datetime.strptime(source_date_raw[:10], date_format).strftime("%d/%m/%Y")
                break
            except ValueError:
                continue
    if not report_date:
        report_date = _text(source_date_value)

    competition_map = {
        "alto": "Alta", "high": "Alta", "medio": "Media", "moderado": "Media",
        "medium": "Media", "bajo": "Baja", "low": "Baja",
    }
    liquidity_map = {
        "alta": "Alta", "high": "Alta", "media": "Media", "moderada": "Media",
        "medium": "Media", "baja": "Baja", "low": "Baja",
    }
    if operation_key == "VENTA":
        primary_candidates = [
            ("Precio publicado de referencia", _communal_market_value(operation_data.get("uf_m2_publicacion_actual"), suffix=" UF/m²")),
            ("Variación de precios publicados · 12 meses", _communal_market_value(operation_data.get("variacion_uf_m2_12m"), suffix="%")),
            ("Propiedades actualmente en oferta", _communal_market_value(operation_data.get("publicaciones_activas"), suffix="publications")),
            ("Competencia", _communal_market_label(indicators.get("nivel_competencia"), competition_map)),
            ("Liquidez", _communal_market_label(indicators.get("liquidez"), liquidity_map)),
        ]
    else:
        primary_candidates = [
            ("Precio de arriendo publicado de referencia", _communal_market_value(operation_data.get("uf_m2_arriendo_actual"), suffix="rental-m2") + " UF/m²" if operation_data.get("uf_m2_arriendo_actual") is not None else ""),
            ("Variación de precios publicados · 12 meses", _communal_market_value(operation_data.get("variacion_arriendo_12m"), suffix="precise-percent") + "%" if operation_data.get("variacion_arriendo_12m") is not None else ""),
            ("Propiedades actualmente en oferta", _communal_market_value(operation_data.get("publicaciones_arriendo_activas"), suffix="publications")),
            ("Competencia", _communal_market_label(indicators.get("nivel_competencia"), competition_map)),
            ("Liquidez", _communal_market_label(indicators.get("liquidez"), liquidity_map)),
        ]
    metrics = [{"label": label, "value": value} for label, value in primary_candidates if value][:5]
    if not metrics:
        return None

    trend_label = _communal_market_label(_text(indicators.get("tendencia_mercado")).casefold(), {
        "desaceleracion": "desaceleración", "desaceleración": "desaceleración",
        "aceleracion": "aceleración", "aceleración": "aceleración",
        "estable": "estabilidad", "crecimiento": "crecimiento",
    })
    competition = next((item[1].casefold() for item in primary_candidates if item[0] == "Competencia" and item[1]), "")
    liquidity = next((item[1].casefold() for item in primary_candidates if item[0] == "Liquidez" and item[1]), "")
    variation_key = "variacion_uf_m2_12m" if operation_key == "VENTA" else "variacion_arriendo_12m"
    variation = _number_from_label(operation_data.get(variation_key))
    interpretation_parts = []
    if competition == "alta" and liquidity == "baja":
        interpretation_parts.append("El mercado muestra una alta cantidad de propiedades compitiendo por compradores y una menor velocidad de absorción.")
    elif competition == "alta":
        interpretation_parts.append("El mercado muestra una alta cantidad de propiedades compitiendo por compradores.")
    elif liquidity == "baja":
        interpretation_parts.append("El mercado presenta una menor velocidad de absorción.")
    elif competition or liquidity:
        signals = ([f"competencia {competition}"] if competition else [])
        if liquidity:
            signals.append(f"liquidez {liquidity}")
        interpretation_parts.append("Los indicadores disponibles muestran " + " y ".join(signals) + ".")
    if variation is not None and variation < 0:
        interpretation_parts.append("Además, los valores publicados han retrocedido durante los últimos 12 meses.")
    elif variation is not None and variation > 0:
        interpretation_parts.append("Además, los valores publicados han aumentado durante los últimos 12 meses.")
    elif trend_label == "desaceleración":
        interpretation_parts.append("Además, el informe identifica una desaceleración en la tendencia del mercado.")
    if interpretation_parts:
        interpretation_parts.append("En este contexto, el posicionamiento de precio adquiere mayor importancia.")
    interpretation = " ".join(interpretation_parts)

    details: list[dict[str, str]] = []
    if operation_key == "VENTA":
        if trend_label:
            details.append({"label": "Tendencia del mercado", "value": trend_label.capitalize()})
        pressure = _communal_market_label(indicators.get("presion_baja_precio"), liquidity_map)
        if pressure:
            details.append({"label": "Presión sobre precios", "value": pressure})
        low = _communal_market_value(ranges.get("min_uf"), suffix="range")
        high = _communal_market_value(ranges.get("max_uf"), suffix="range")
        if low and high:
            details.append({"label": "Rango observado", "value": f"{low}–{high} UF"})
        total = _communal_market_value(operation_data.get("publicaciones_totales"), suffix="publications")
        if total:
            details.append({"label": "Publicaciones observadas", "value": total})
    else:
        total = _communal_market_value(operation_data.get("publicaciones_arriendo_totales"), suffix="publications")
        if total:
            details.append({"label": "Publicaciones de arriendo totales", "value": total})
        low = _communal_market_value(ranges.get("min_uf"), suffix="range")
        high = _communal_market_value(ranges.get("max_uf"), suffix="range")
        if low and high:
            details.append({"label": "Rango de precios observado", "value": f"{low}–{high} UF"})
    return {
        "kind": "COMMUNAL_MARKET_REPORT", "title": _communal_market_title(property_type, commune),
        "metrics": metrics, "interpretation": interpretation, "details": details,
        "commune": commune, "property_type": property_type,
        "operation": operation_key,
        "reference_value_uf_m2": operation_data.get("uf_m2_publicacion_actual") if operation_key == "VENTA" else None,
        "currency_basis": _position_currency_basis(record.get("currency_basis") or record.get("reference_unit") or "UF"),
        "reference_unit": record.get("reference_unit") or "UF/m²",
        "area_basis": record.get("area_basis") or record.get("reference_area_basis") or record.get("surface_basis") or "",
        "source_label": f"Informe comunal · {commune} · {property_type}" + (f" · Corte {report_date}" if report_date else ""),
        "source_date": report_date, "document_url": document_url,
        "document_title": "Informe de mercado comunal",
        "document_metadata": " · ".join(
            part for part in (commune, "PDF", f"Corte {report_date}" if report_date else "") if part
        ),
    }


def _communal_match_key(commune: Any, property_type: Any) -> str:
    """Normalize only case, diacritics and whitespace before exact key matching."""
    def normalize_part(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
        text = "".join(char for char in text if not unicodedata.combining(char))
        return re.sub(r"\s+", " ", text).strip()

    normalized_commune = normalize_part(commune)
    normalized_type = normalize_part(property_type)
    if not normalized_commune or not normalized_type:
        return ""
    return f"{normalized_commune}|{normalized_type}"


def _verified_master_identity(db: Any, property_code: str) -> dict[str, str]:
    """Read identity labels only from the canonical property with the exact code."""
    code = str(property_code or "").strip()
    if not code:
        return {}
    projection = {
        "_id": 0,
        "codigo": 1,
        "ubicacion.comuna": 1,
        "ubicacion.region": 1,
        "metadata.tipo_propiedad": 1,
    }
    try:
        matches = list(db["universo_cartera_prop360"].find({"codigo": code}, projection).limit(2))
    except Exception:
        return {}
    if len(matches) != 1 or str(matches[0].get("codigo") or "").strip() != code:
        return {}
    master = matches[0]
    location = master.get("ubicacion") if isinstance(master.get("ubicacion"), Mapping) else {}
    metadata = master.get("metadata") if isinstance(master.get("metadata"), Mapping) else {}
    return {
        "commune": _text(location.get("comuna")),
        "region": _text(location.get("region")),
        "property_type": _text(metadata.get("tipo_propiedad")),
    }


def _verified_master_comparable_area_semantics(db: Any, property_code: str) -> dict[str, Any]:
    """Read area semantics from the unique canonical master with this exact code."""
    code = str(property_code or "").strip()
    if not code:
        return {}
    projection = {
        "_id": 0, "codigo": 1,
        "analisis_comparables.mercado.price_surface": 1,
        "analisis_comparables.client_evidence.primary_indicator": 1,
    }
    try:
        matches = list(db["universo_cartera_prop360"].find({"codigo": code}, projection).limit(2))
    except Exception:
        return {}
    if len(matches) != 1 or str(matches[0].get("codigo") or "").strip() != code:
        return {}
    analysis = matches[0].get("analisis_comparables")
    if not isinstance(analysis, Mapping):
        return {}
    market = analysis.get("mercado")
    client = analysis.get("client_evidence")
    if not isinstance(market, Mapping) or not isinstance(client, Mapping):
        return {}
    price_surface = _text(market.get("price_surface"))
    primary_indicator = _text(client.get("primary_indicator"))
    if not price_surface and not primary_indicator:
        return {}
    return {"price_surface": price_surface, "primary_indicator": primary_indicator}


_PUBLICATION_CHANNELS: tuple[tuple[str, str], ...] = (
    ("procasa", "PROCASA"),
    ("portal_inmobiliario", "Portal Inmobiliario"),
    ("toctoc", "TocToc"),
    ("yapo", "Yapo"),
    ("chilepropiedades", "ChilePropiedades"),
    ("enlace_inmobiliario", "Enlace Inmobiliario"),
    ("proppit", "Proppit"),
)
_PROPPIT_NETWORK_PORTALS: tuple[str, ...] = ("icasas", "Mitula", "Nestoria", "Nuroa", "Trovit")
_PUBLICATION_TELEMETRY_PORTALS = {
    "procasa": "PROCASA",
    "portal_inmobiliario": "PortalInmobiliario",
    "mercado_libre": "MercadoLibre",
    "mercadolibre": "MercadoLibre",
    "toctoc": "TOCTOC",
    "yapo": "Yapo",
    "chilepropiedades": "ChilePropiedades",
    "proppit": "Proppit",
    "enlace_inmobiliario": "EnlaceInmobiliario",
}
_PUBLICATION_LOGO_ASSETS: dict[str, tuple[str, ...]] = {
    "PROCASA": ("/static/favicon_procasa_mark.png",),
    "PORTAL_INMOBILIARIO": ("/static/portal-logos/portalinmobiliario.svg",),
    "MERCADO_LIBRE": ("/static/portal-logos/mercado-libre.svg",),
    "TOCTOC": ("/static/portal-logos/toctoc.svg",),
    "YAPO": ("/static/portal-logos/yapo.svg",),
    "PROPPIT": ("/static/portal-logos/proppit.png",),
    "CHILEPROPIEDADES": ("/static/portal-logos/chilepropiedades.svg",),
    "ENLACE_INMOBILIARIO": ("/static/portal-logos/enlace-inmobiliario.png",),
}
_PUBLICATION_VERIFICATION_TTL = timedelta(days=30)


def _safe_publication_url(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    return raw


def _publication_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _publication_records(portal: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records = portal.get("publicaciones")
    values: list[Mapping[str, Any]] = []
    if isinstance(records, Mapping):
        values.extend(item for item in records.values() if isinstance(item, Mapping))
    if not values and isinstance(portal.get("publicada"), bool):
        values.append(portal)
    return values


def _publication_links(portal_key: str, portal: Mapping[str, Any], records: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    telemetry_portal = _PUBLICATION_TELEMETRY_PORTALS.get(portal_key, "Other")

    def add(label: str, value: Any) -> None:
        url = _safe_publication_url(value)
        if url and all(item["url"] != url for item in links):
            links.append({"label": label, "url": url, "external_portal": telemetry_portal})

    # Prop360 stores Portal Inmobiliario and Mercado Libre as one publication
    # source. Keep them one channel while preserving each explicit public URL.
    if portal_key == "portal_inmobiliario":
        pi_values = portal.get("urls_pi")
        ml_values = portal.get("urls_mercado_libre")
        if isinstance(pi_values, (list, tuple)):
            for value in pi_values:
                add("Ver en Portal Inmobiliario", value)
        add("Ver en Portal Inmobiliario", portal.get("url_pi"))
        if isinstance(ml_values, (list, tuple)):
            for value in ml_values:
                add("Ver en Mercado Libre", value)
        add("Ver en Mercado Libre", portal.get("url_mercado_libre"))

    for record in records:
        raw_url = record.get("url") or record.get("url_publicacion")
        label = "Ver publicación"
        host = (urlsplit(_safe_publication_url(raw_url)).hostname or "").casefold()
        if portal_key == "portal_inmobiliario":
            label = "Ver Mercado Libre" if "mercadolibre" in host else "Ver Portal Inmobiliario" if "portalinmobiliario" in host else label
        add(label, raw_url)
    return links


def _publication_item(
    portal_key: str, portal: Mapping[str, Any], owner_label: str,
    records: list[Mapping[str, Any]], *, now: datetime, kind: str | None = None,
    links: list[dict[str, str]] | None = None,
    group_id: str | None = None,
) -> dict[str, Any] | None:
    published_records = [record for record in records if record.get("publicada") is True]
    if not published_records:
        return None
    item_links = links if links is not None else _publication_links(portal_key, portal, published_records)
    checked_at = max(
        (stamp for record in published_records for key in ("verified_at", "last_verified_at", "fecha_verificacion")
         if (stamp := _publication_datetime(record.get(key))) is not None),
        default=None,
    )
    verification_marked = any(
        _text(record.get("verification_status")).casefold() in {"verified", "verificado", "active_verified"}
        for record in published_records
    )
    recently_verified = bool(
        verification_marked and checked_at is not None
        and timedelta(0) <= now - checked_at <= _PUBLICATION_VERIFICATION_TTL
    )
    status = "Publicado"
    return {
        "kind": kind or portal_key.upper(),
        "group_id": group_id or portal_key,
        "owner_label": owner_label,
        "published": True,
        "status": status,
        "link_status": "" if item_links else "Enlace no disponible",
        "url": item_links[0]["url"] if item_links else "",
        "links": item_links,
        "verified_at": checked_at if recently_verified else None,
        "verification_status": "VERIFIED" if recently_verified else "PUBLISHED",
        "logo_urls": _PUBLICATION_LOGO_ASSETS.get(kind or portal_key.upper(), ()),
        "network_portals": list(_PROPPIT_NETWORK_PORTALS) if portal_key == "proppit" else [],
    }


def _portal_inmobiliario_items(
    portal: Mapping[str, Any], records: list[Mapping[str, Any]], *, now: datetime,
) -> list[dict[str, Any]]:
    """Expose the shared PI/ML source as two independently identified channels."""
    active_records = [record for record in records if record.get("publicada") is True]
    if not active_records:
        return []
    channel_links: dict[str, list[dict[str, str]]] = {
        "PORTAL_INMOBILIARIO": [], "MERCADO_LIBRE": [],
    }

    def add(kind: str, label: str, raw_url: Any) -> None:
        url = _safe_publication_url(raw_url)
        if url and all(item["url"] != url for item in channel_links[kind]):
            portal = "MercadoLibre" if kind == "MERCADO_LIBRE" else "PortalInmobiliario"
            channel_links[kind].append({"label": label, "url": url, "external_portal": portal})

    pi_values = portal.get("urls_pi")
    if isinstance(pi_values, (list, tuple)):
        for value in pi_values:
            add("PORTAL_INMOBILIARIO", "Ver Portal Inmobiliario", value)
    add("PORTAL_INMOBILIARIO", "Ver Portal Inmobiliario", portal.get("url_pi"))
    ml_values = portal.get("urls_mercado_libre")
    if isinstance(ml_values, (list, tuple)):
        for value in ml_values:
            add("MERCADO_LIBRE", "Ver Mercado Libre", value)
    add("MERCADO_LIBRE", "Ver Mercado Libre", portal.get("url_mercado_libre"))
    for record in active_records:
        raw_url = record.get("url") or record.get("url_publicacion")
        host = (urlsplit(_safe_publication_url(raw_url)).hostname or "").casefold()
        if "mercadolibre" in host:
            add("MERCADO_LIBRE", "Ver Mercado Libre", raw_url)
        elif "portalinmobiliario" in host:
            add("PORTAL_INMOBILIARIO", "Ver Portal Inmobiliario", raw_url)

    pi_item = _publication_item(
        "portal_inmobiliario", portal, "Portal Inmobiliario", active_records,
        now=now, kind="PORTAL_INMOBILIARIO", links=channel_links["PORTAL_INMOBILIARIO"],
        group_id="portal_inmobiliario",
    )
    ml_item = _publication_item(
        "mercado_libre", portal, "Mercado Libre", active_records,
        now=now, kind="MERCADO_LIBRE", links=channel_links["MERCADO_LIBRE"],
        group_id="portal_inmobiliario",
    )
    return [item for item in (pi_item, ml_item) if item]


def _publication_presence_model(property_doc: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Normalize only active publication evidence from one exact Prop360 record."""
    publications = property_doc.get("publicaciones")
    if not isinstance(publications, Mapping):
        publications = {}
    now_utc = _publication_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    known_keys = {key for key, _ in _PUBLICATION_CHANNELS} | {"mercadolibre", "mercado_libre"}
    for portal_key, label in _PUBLICATION_CHANNELS:
        portal = publications.get(portal_key)
        if not isinstance(portal, Mapping):
            portal = {}
        portal = dict(portal)
        records = _publication_records(portal)
        if portal_key == "portal_inmobiliario":
            for alias_key in ("mercadolibre", "mercado_libre"):
                alias = publications.get(alias_key)
                if not isinstance(alias, Mapping):
                    continue
                for field in ("url_pi", "urls_pi", "url_mercado_libre", "urls_mercado_libre"):
                    if field in alias and field not in portal:
                        portal[field] = alias[field]
                records.extend(_publication_records(alias))
            items.extend(_portal_inmobiliario_items(portal, records, now=now_utc))
            continue
        item = _publication_item(portal_key, portal, label, records, now=now_utc)
        if item:
            items.append(item)

    # Keep Enlace Inmobiliario visible as a separate future channel until its
    # publication status and URL are supplied by the canonical property record.
    if not any(item["kind"] == "ENLACE_INMOBILIARIO" for item in items):
        items.append({
            "kind": "ENLACE_INMOBILIARIO",
            "group_id": "enlace_inmobiliario",
            "owner_label": "Enlace Inmobiliario",
            "published": False,
            "status": "Sin publicación registrada",
            "link_status": "Enlace aún no disponible",
            "detail_label": "Enlace aún no disponible",
            "url": "",
            "links": [],
            "verified_at": None,
            "verification_status": "NOT_RECORDED",
            "logo_urls": _PUBLICATION_LOGO_ASSETS["ENLACE_INMOBILIARIO"],
            "network_portals": [],
        })

    # Preserve future canonical channels without leaking their raw Mongo shape.
    for portal_key, portal in publications.items():
        key = _text(portal_key).casefold()
        if key in known_keys or not isinstance(portal, Mapping):
            continue
        records = _publication_records(portal)
        if not records:
            continue
        label = re.sub(r"[_-]+", " ", _text(portal_key)).strip().title()
        item = _publication_item(key, portal, label, records, now=now_utc)
        if item:
            item["kind"] = key.upper()
            items.append(item)
    # Keep the distribution-network row last, after standalone and future
    # canonical channels.
    items.sort(key=lambda item: item.get("kind") == "PROPPIT")
    distinct_urls = {link["url"] for item in items if item.get("published") for link in item["links"]}
    published_items = [item for item in items if item.get("published")]
    group_ids = {item["group_id"] for item in published_items}
    base_publication_count = len(group_ids)
    proppit_extra_channels = len(_PROPPIT_NETWORK_PORTALS) if any(
        item["kind"] == "PROPPIT" and item.get("published") for item in published_items
    ) else 0
    publication_channel_count = len(published_items) + proppit_extra_channels
    available_link_count = len(distinct_urls)
    return {
        "source": "universo_cartera_prop360.publicaciones",
        "source_status": "VERIFIED",
        "publication_group_count": base_publication_count,
        "base_publication_count": base_publication_count,
        "publication_channel_count": publication_channel_count,
        "portal_channel_count": publication_channel_count,
        "available_link_count": available_link_count,
        "owner_facing_row_count": len(items),
        "total_published_channels": publication_channel_count,
        "channels_with_url": available_link_count,
        "items": items,
    }


def resolve_property_publication_presence(db: Any, property_code: Any) -> dict[str, Any]:
    """Read current channel presence from the exact canonical master property."""
    code = _text(property_code)
    empty = {
        "source": "universo_cartera_prop360.publicaciones",
        "source_status": "IDENTITY_UNRESOLVED",
        "publication_group_count": 0,
        "base_publication_count": 0,
        "publication_channel_count": 0,
        "portal_channel_count": 0,
        "available_link_count": 0,
        "owner_facing_row_count": 0,
        "total_published_channels": 0,
        "channels_with_url": 0,
        "items": [],
    }
    if not code:
        return empty
    try:
        matches = list(db["universo_cartera_prop360"].find(
            {"codigo": code}, {"_id": 0, "codigo": 1, "publicaciones": 1},
        ).limit(2))
    except Exception:
        return {**empty, "source_status": "SOURCE_ERROR"}
    if len(matches) != 1 or _text(matches[0].get("codigo")) != code:
        return empty
    return _publication_presence_model(matches[0])


def _number_from_label(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        import math
        return float(value) if math.isfinite(value) else None
    match = _NUMBER.search(_text(value))
    if not match:
        return None
    normalized = match.group(1).replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _position_unit_label(value: Any) -> str:
    """Return a unit only when it is explicitly present in a comparable label."""
    raw = _text(value).casefold().replace("m2", "m²")
    compact = re.sub(r"\s+", "", raw)
    if re.search(r"(uf|clp)/m²(?:útil|util|useful)/(?:built|construido|construida)", compact):
        currency_match = re.search(r"(uf|clp)/m²", compact)
        currency = currency_match.group(1).upper() if currency_match else ""
        return f"{currency}/m² útil/construido"
    if re.search(r"\$[\d.,]+/m²/mes", compact):
        return "$/m²/mes"
    if re.search(r"[\d.,]+clp/m²/mes", compact):
        return "CLP/m²/mes"
    match = re.search(r"(uf|clp)/m²(útil|util|useful|built|construido|construida|total|terreno|land)?(/mes)?", compact)
    if match:
        currency, basis, monthly = match.groups()
        basis = "útil" if basis in {"util", "útil", "useful"} else "construido" if basis == "built" else "terreno" if basis == "land" else basis or ""
        return f"{currency.upper()}/m²{(' ' + basis) if basis else ''}{'/mes' if monthly else ''}"
    if re.search(r"\$[\d.,]+/m²(?:/mes)?", compact):
        return "$/m²/mes" if "/mes" in compact else "$/m²"
    return ""


def _position_gap(reference: Any, subject: Any, unit: str = "") -> float | None:
    """A display-only ratio, only for positively identified compatible units."""
    left, right = _position_unit_label(reference), _position_unit_label(subject)
    # An explicit shared unit binds bare numeric values; conflicting labels fail closed.
    shared = _position_unit_label(unit)
    left = left or shared
    right = right or shared
    if not left or left != right:
        return None
    ref, value = _number_from_label(reference), _number_from_label(subject)
    if ref is None or value is None or ref <= 0 or value < 0:
        return None
    return (value / ref - 1) * 100


def _position_unit_key(value: Any) -> str:
    label = _position_unit_label(value)
    raw = label.casefold().replace(" ", "")
    if raw in {"$/m²/mes", "clp/m²/mes"}:
        return raw
    if raw in {"uf/m²útil/construido", "uf/m²útil/built", "clp/m²útil/construido", "clp/m²útil/built"}:
        return raw
    raw = raw.replace("deoferta", "")
    match = re.fullmatch(r"(uf|clp|\$)/m²(?:útil|util|useful|built|construido|construida|total|terreno|land)?", raw)
    return match.group(0).replace("util", "útil") if match else ""


def _position_unit_dimension(value: Any) -> str:
    key = _position_unit_key(value)
    if key.endswith("/mes"):
        return key
    return next((unit for unit in ("uf/m²", "clp/m²", "$/m²") if key.startswith(unit)), "")


def _position_area_basis(value: Any = "", explicit: Any = None) -> str:
    """Return an area basis only when the source states it explicitly."""
    def normalize(source: Any) -> str:
        raw = _text(source).casefold().replace("á", "a").replace("ú", "u")
        canonical = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        if canonical == "comparable_surface_ref":
            return "COMPARABLE_SURFACE_REF"
        # Historical comparable snapshots sometimes explicitly label both
        # sides of the same denominator as útil/construido. Preserve that as
        # one opaque, shared basis; never reinterpret it as USEFUL or BUILT.
        if re.search(r"util\s*/\s*(?:built|construid[oa]?)", raw):
            return "COMPARABLE_SURFACE_REF"
        compact = re.sub(r"[^a-z]+", " ", raw).strip()
        if re.search(r"\b(util|utile|useful)\b", compact):
            return "USEFUL"
        if re.search(r"\b(built|construid[oa]?)\b", compact):
            return "BUILT"
        if re.search(r"\b(land|terreno)\b", compact):
            return "LAND"
        return ""
    from_unit, from_field = normalize(value), normalize(explicit)
    if from_unit and from_field and from_unit != from_field:
        return ""
    return from_field or from_unit


def _position_currency_basis(value: Any = "", explicit: Any = None) -> str:
    raw = _text(explicit or value).casefold()
    if re.search(r"\buf\b", raw):
        return "UF"
    if re.search(r"\bclp\b|\$", raw):
        return "CLP"
    return ""


def _position_basis_unit(currency: str, area_basis: str) -> str:
    area_label = {
        "USEFUL": "útil", "BUILT": "construido", "LAND": "terreno",
        "COMPARABLE_SURFACE_REF": "útil/construido",
    }.get(area_basis, "")
    return f"{currency}/m² {area_label}".strip() if currency and area_label else ""


def _position_measurement_definition(unit: Any = "", explicit: Any = None) -> str:
    """Normalize the meaning of a plotted UF/m² value without guessing its area basis."""
    declared = re.sub(r"[^A-Z0-9]+", "_", _text(explicit).upper()).strip("_")
    if declared:
        return declared
    if _position_currency_basis(unit) == "UF" and _position_unit_key(unit).startswith("uf/m²") and not _position_unit_key(unit).endswith("/mes"):
        return "UF_PER_M2"
    return ""


def are_market_position_scales_compatible(source_a: Mapping[str, Any], source_b: Mapping[str, Any]) -> bool:
    """Require the same currency, explicit area basis, and measurement meaning."""
    currency_a = _position_currency_basis(source_a.get("unit"), source_a.get("currency_basis"))
    currency_b = _position_currency_basis(source_b.get("unit"), source_b.get("currency_basis"))
    basis_a = _position_area_basis(source_a.get("unit"), source_a.get("area_basis"))
    basis_b = _position_area_basis(source_b.get("unit"), source_b.get("area_basis"))
    measure_a = _position_measurement_definition(source_a.get("unit"), source_a.get("measurement_definition"))
    measure_b = _position_measurement_definition(source_b.get("unit"), source_b.get("measurement_definition"))
    return bool(
        currency_a and currency_a == currency_b
        and basis_a and basis_a == basis_b
        and measure_a and measure_a == measure_b
    )


def _format_position_value(value: Any) -> str:
    number = _number_from_label(value)
    if number is None:
        return ""
    whole, _, fraction = f"{number:,.1f}".partition(".")
    return whole.replace(",", ".") + "," + fraction


def _position_scale(values: list[float]) -> tuple[float, float] | None:
    valid = [value for value in values if isinstance(value, (int, float)) and value >= 0 and value < float("inf")]
    if len(valid) < 2:
        return None
    low, high = min(valid), max(valid)
    span = high - low
    padding = max(span * 0.14, abs(high) * 0.06, 0.1)
    return low - padding, high + padding


def _position_scale_pct(value: float, domain: tuple[float, float]) -> float:
    low, high = domain
    if high <= low:
        return 50.0
    return max(3.0, min(97.0, (value - low) / (high - low) * 100))


def _position_simulation_data(
    *, position: Mapping[str, Any], comparables: Mapping[str, Any],
    communal: Mapping[str, Any], property_state: Mapping[str, Any],
    current_price: Any, recommended_price: Any, current_price_label: Any,
    recommended_price_label: Any, adjustment: Any, recommendation_is_monthly: bool, stale: bool,
) -> dict[str, Any]:
    """Build a visual-only scenario from one frozen comparable snapshot.

    It intentionally does not inspect master-property/current comparables data.
    The adjusted value uses the exact recorded surface denominator and is
    cross-checked against the canonical current UF/m² before it is exposed.
    """
    unavailable = {"available": False, "reason": "SNAPSHOT_EVIDENCE_INSUFFICIENT"}
    if stale or not isinstance(comparables, Mapping):
        return unavailable
    if property_state.get("current_price") is None or recommended_price is None or not recommendation_is_monthly:
        return {**unavailable, "reason": "PRICE_VALUES_NOT_FROZEN_IN_MONTHLY_SNAPSHOT"}
    historical_email = comparables.get("source") == "VERIFIED_SENT_EMAIL"
    evidence_level = str(comparables.get("evidence_level") or "").upper()
    if historical_email:
        if str(comparables.get("campaign_comparable_mode") or "").upper() != "PRIMARY_COMPARABLES":
            return {**unavailable, "reason": "HISTORICAL_PRIMARY_COMPARABLE_EVIDENCE_MISSING"}
        evidence_level = "VERIFIED_SENT_EMAIL"
    elif evidence_level not in {"HIGH", "MEDIUM"}:
        return {**unavailable, "reason": "COMPARABLE_EVIDENCE_NOT_STRONG_ENOUGH"}
    if comparables.get("evidence_conflict") is True or str(comparables.get("display_status") or "OK").upper() == "REVIEW":
        return {**unavailable, "reason": "COMPARABLE_EVIDENCE_REQUIRES_REVIEW"}
    version = _text(comparables.get("version") or comparables.get("algorithm_version"))
    if not historical_email and version and "cluster_v2" not in version.casefold():
        return {**unavailable, "reason": "COMPARABLE_SNAPSHOT_NOT_CANONICAL_CLUSTER_V2"}
    if str(comparables.get("positioning_mode") or "PRICE_M2").upper() != "PRICE_M2":
        return {**unavailable, "reason": "COMPARABLE_POSITIONING_NOT_PER_SQUARE_METER"}
    try:
        count = int(_number_from_label(comparables.get("count") or comparables.get("selected_n")) or 0)
    except (TypeError, ValueError):
        count = 0
    if count < 5:
        return {**unavailable, "reason": "COMPARABLE_SAMPLE_TOO_SMALL"}

    unit = _text(comparables.get("unit") or comparables.get("positioning_unit_label"))
    unit_key = _position_unit_key(unit)
    # This interactive view is specifically UF/m² and does not simulate total-price,
    # CLP, or rentals with a different denominator.
    if not unit_key.startswith("uf/m²") or "UF" not in _text(current_price_label).upper() or "UF" not in _text(recommended_price_label).upper():
        return {**unavailable, "reason": "POSITIONING_UNIT_OR_PRICE_CURRENCY_INCOMPATIBLE"}
    if str(property_state.get("operation") or "").strip().upper() not in {"VENTA", "VENTA "}:
        return {**unavailable, "reason": "POSITIONING_OPERATION_NOT_SUPPORTED"}

    median = _number_from_label(comparables.get("reference_value", comparables.get("positioning_reference_value")))
    current_m2 = _number_from_label(comparables.get("property_value", comparables.get("positioning_property_value")))
    surface = _number_from_label(comparables.get("surface_ref_m2", comparables.get("surface_used_m2")))
    property_surface = _number_from_label(property_state.get("surface_ref_m2"))
    if surface is not None and property_surface is not None and abs(surface - property_surface) > max(0.01, surface * 0.001):
        return {**unavailable, "reason": "POSITIONING_SURFACE_DENOMINATOR_MISMATCH"}
    if surface is None:
        surface = property_surface
    price = _number_from_label(current_price)
    proposed = _number_from_label(recommended_price)
    adjustment_number = _number_from_label(adjustment)
    if any(value is None or value <= 0 for value in (median, current_m2, surface, price, proposed)):
        return {**unavailable, "reason": "POSITIONING_REQUIRED_VALUE_MISSING"}
    if adjustment_number is None or not 0 < abs(adjustment_number) <= 100:
        return {**unavailable, "reason": "POSITIONING_ADJUSTMENT_MISSING"}
    calculated_current = price / surface
    # Canonical property_value is rounded to one decimal in the email. A wider
    # disagreement indicates a different surface denominator or stale data.
    if abs(calculated_current - current_m2) > max(0.11, current_m2 * 0.001):
        return {**unavailable, "reason": "POSITIONING_SURFACE_DENOMINATOR_MISMATCH"}
    proposed_m2 = proposed / surface
    current_gap = (current_m2 / median - 1) * 100
    proposed_gap = (proposed_m2 / median - 1) * 100

    communal_value: float | None = None
    communal_unit = ""
    if isinstance(communal, Mapping):
        candidate_unit = _text(communal.get("unit") or communal.get("reference_unit"))
        candidate_key = _position_unit_key(candidate_unit)
        communal_metrics = communal.get("relevant_metrics") if isinstance(communal.get("relevant_metrics"), Mapping) else {}
        # Accept only a numeric, explicitly typed figure from the frozen snapshot.
        # Narrative summaries are intentionally not parsed into chart data.
        candidates = [
            (communal.get(key), candidate_unit, candidate_key)
            for key in ("offer_uf_m2", "uf_m2_offer", "value_uf_m2", "reference_value_uf_m2", "reference_value", "value")
        ]
        candidates.extend(
            (communal_metrics.get(key), "UF/m² de oferta", "uf/m²")
            for key in ("uf_m2_publicacion_actual", "uf_m2_arriendo_actual")
            if communal_metrics.get(key) is not None
        )
        for candidate_value, candidate_label, candidate_unit_key in candidates:
            candidate = _number_from_label(candidate_value)
            if candidate is not None and candidate > 0 and candidate_unit_key:
                communal_value, communal_unit = candidate, candidate_label
                break

    chart_values = [median, current_m2, proposed_m2]
    # Both are plotted only when the numeric currency/area dimension agrees.
    # The distinct denominator/source wording stays visible in the legend, and
    # communal values are never used in the comparable-gap calculation.
    communal_on_same_scale = bool(
        communal_value is not None
        and _position_unit_dimension(communal_unit) == _position_unit_dimension(unit)
    )
    if communal_on_same_scale and communal_value is not None:
        chart_values.append(communal_value)
    domain = _position_scale(chart_values)
    if domain is None:
        return {**unavailable, "reason": "POSITIONING_SCALE_UNAVAILABLE"}

    def gap_label(value: float) -> str:
        rounded = int(round(value))
        if rounded == 0:
            return "En línea con propiedades similares"
        relation = "sobre" if rounded > 0 else "bajo"
        return f"{abs(rounded)}% {relation} propiedades similares"

    adjustment_label = f"-{abs(adjustment_number):g}%"
    source_date = _source_date_label(comparables.get("source_date") or comparables.get("cutoff_date"))
    analysis_date = _source_date_label(comparables.get("analysis_generated_at"))
    communal_date = _source_date_label(communal.get("source_date") or communal.get("cutoff_date")) if isinstance(communal, Mapping) else ""
    communal_count = _text(communal.get("universe_value") or communal.get("active_listing_count")) if isinstance(communal, Mapping) else ""
    communal_count_unit = _text(communal.get("universe_unit") or "publicaciones activas") if communal_count else ""
    current_m2_label = _format_position_value(current_m2)
    proposed_m2_label = _format_position_value(proposed_m2)
    median_label = _format_position_value(median)
    communal_label = _format_position_value(communal_value) if communal_value is not None else ""
    current_copy = (
        f"Actualmente, tu propiedad se publica en {current_m2_label} {unit}, aproximadamente "
        f"un {abs(current_gap):.0f}% {'por sobre el valor observado en' if current_gap > 0 else 'por debajo del valor observado en' if current_gap < 0 else 'en línea con'} "
        "propiedades similares."
    )
    if proposed_gap < current_gap:
        adjusted_copy = (
            f"Con el ajuste recomendado, la publicación quedaría cerca de {proposed_m2_label} {unit} "
            f"y la diferencia frente a propiedades similares bajaría aproximadamente a {abs(proposed_gap):.0f}%."
        )
    else:
        adjusted_copy = (
            f"Con el ajuste recomendado, la publicación quedaría cerca de {proposed_m2_label} {unit}, "
            f"con una diferencia aproximada de {abs(proposed_gap):.0f}% frente a propiedades similares."
        )
    return {
        "available": True,
        "reason": "",
        "evidence_level": evidence_level,
        "count": count,
        "unit": unit,
        "surface_ref_m2": surface,
        "adjustment_label": adjustment_label,
        "current_m2": current_m2,
        "proposed_m2": proposed_m2,
        "current_m2_label": current_m2_label,
        "proposed_m2_label": proposed_m2_label,
        "median": median,
        "median_label": median_label,
        "communal_value": communal_value,
        "communal_label": communal_label,
        "communal_unit": communal_unit,
        "communal_on_same_scale": communal_on_same_scale,
        "communal_count": communal_count,
        "communal_count_unit": communal_count_unit,
        "current_gap_pct": current_gap,
        "proposed_gap_pct": proposed_gap,
        "current_gap_label": gap_label(current_gap),
        "proposed_gap_label": gap_label(proposed_gap),
        "current_copy": current_copy,
        "adjusted_copy": adjusted_copy,
        "current_x": _position_scale_pct(current_m2, domain),
        "proposed_x": _position_scale_pct(proposed_m2, domain),
        "median_x": _position_scale_pct(median, domain),
        "communal_x": _position_scale_pct(communal_value, domain) if communal_on_same_scale and communal_value is not None else None,
        "gap_left": min(_position_scale_pct(median, domain), _position_scale_pct(current_m2, domain)),
        "gap_width": abs(_position_scale_pct(current_m2, domain) - _position_scale_pct(median, domain)),
        "communal_source_date": communal_date,
        "comparables_source_date": source_date,
        "comparables_analysis_date": analysis_date,
    }


def _market_position_data(
    *, position_simulation: Mapping[str, Any], position_static: Mapping[str, Any],
    comparables: Mapping[str, Any], communal: Mapping[str, Any],
    appraisal_card: Mapping[str, Any] | None, property_state: Mapping[str, Any],
    property_code: str, current_price: Any, recommended_price: Any,
    recommendation_is_monthly: bool, stale: bool,
) -> dict[str, Any]:
    """Combine only property-scoped references that share a verified UF/m² scale."""
    unavailable = {"available": False, "simulation_available": False, "references": []}

    appraisal_source = appraisal_card.get("market_position_reference") if isinstance(appraisal_card, Mapping) else None
    appraisal_available = bool(appraisal_card)
    appraisal_on_scale = False
    appraisal_exclusion_reason = "NO_VERIFIED_STRUCTURED_REFERENCE" if appraisal_available else "APPRAISAL_NOT_AVAILABLE"
    communal_source_value = None
    communal_source_unit = None
    communal_source_basis = None
    if isinstance(communal, Mapping):
        source_metrics = communal.get("relevant_metrics") if isinstance(communal.get("relevant_metrics"), Mapping) else {}
        for source_key in ("offer_uf_m2", "uf_m2_offer", "value_uf_m2", "reference_value_uf_m2", "reference_value", "value", "uf_m2_publicacion_actual"):
            raw = communal.get(source_key, source_metrics.get(source_key))
            number = _number_from_label(raw)
            if number is not None and number > 0:
                communal_source_value = number
                communal_source_unit = communal.get("reference_unit") or communal.get("unit")
                communal_source_basis = communal.get("area_basis") or communal.get("reference_area_basis") or communal.get("surface_basis")
                break
    communal_available = communal_source_value is not None
    communal_on_scale = False
    communal_exclusion_reason = "COMMUNAL_REFERENCE_NOT_AVAILABLE" if not communal_available else "AREA_BASIS_UNKNOWN"

    def diagnostics() -> dict[str, Any]:
        return {
            "appraisal_available": appraisal_available,
            "appraisal_on_scale": appraisal_on_scale,
            "appraisal_exclusion_reason": appraisal_exclusion_reason,
            "communal_available": communal_available,
            "communal_on_scale": communal_on_scale,
            "communal_exclusion_reason": communal_exclusion_reason,
        }

    def unavailable_result(reason: str) -> dict[str, Any]:
        return {**unavailable, "reason": reason, **diagnostics()}

    if stale:
        if appraisal_available:
            appraisal_exclusion_reason = "STALE_PORTAL"
        if communal_available:
            communal_exclusion_reason = "STALE_PORTAL"
        return unavailable_result("STALE_PORTAL")

    comparable_ok = bool(position_static.get("available"))
    scale_unit = _position_unit_label(position_static.get("unit")) if comparable_ok else ""
    scale_currency = _position_currency_basis(scale_unit) if scale_unit else ""
    scale_area_basis = _position_area_basis(scale_unit) if scale_unit else ""
    if scale_currency != "UF" or not scale_area_basis:
        comparable_ok = False
        scale_unit = scale_currency = scale_area_basis = ""
    owner_unit = _text(position_static.get("owner_unit") or scale_unit) or "UF/m²"
    comparable_surface = _number_from_label(
        comparables.get("surface_ref_m2", comparables.get("surface_used_m2"))
    ) if comparable_ok and isinstance(comparables, Mapping) else None
    property_surface = _number_from_label(property_state.get("surface_ref_m2"))
    if comparable_surface and property_surface and abs(comparable_surface - property_surface) > max(0.01, comparable_surface * 0.001):
        comparable_ok = False
        scale_unit = ""

    references: list[dict[str, Any]] = []
    if comparable_ok:
        median = _number_from_label(position_static.get("reference_value"))
        if median and median > 0:
            references.append({
                "kind": "COMPARABLE", "label": "Propiedades similares",
                "value": median, "value_label": _format_position_value(median),
                "unit": scale_unit, "area_basis": scale_area_basis, "currency_basis": scale_currency,
                "measurement_definition": _position_measurement_definition(scale_unit),
                "count": position_static.get("count"),
            })

    if (
        isinstance(appraisal_source, Mapping)
        and appraisal_source.get("verified") is True
        and str(appraisal_source.get("property_code") or "") == str(property_code)
    ):
        appraisal_mid = _number_from_label(appraisal_source.get("appraisal_value_uf"))
        explicit_m2 = _number_from_label(appraisal_source.get("appraisal_uf_m2"))
        explicit_raw_unit = _text(appraisal_source.get("appraisal_uf_m2_unit"))
        appraisal_currency = _position_currency_basis(
            explicit_raw_unit, appraisal_source.get("currency_basis")
        )
        appraisal_area_basis = _position_area_basis(
            explicit_raw_unit, appraisal_source.get("area_basis")
        )
        if explicit_m2 is None:
            appraisal_exclusion_reason = "NO_STRUCTURED_VALUE"
        elif appraisal_currency != "UF":
            appraisal_exclusion_reason = "CURRENCY_BASIS_UNKNOWN_OR_UNSUPPORTED"
        elif not appraisal_area_basis:
            appraisal_exclusion_reason = "AREA_BASIS_UNKNOWN"
        elif scale_area_basis and appraisal_area_basis != scale_area_basis:
            appraisal_exclusion_reason = "INCOMPATIBLE_AREA_BASIS"
        explicit_unit = _position_basis_unit(appraisal_currency, appraisal_area_basis)
        appraisal_measurement = _position_measurement_definition(
            explicit_raw_unit, appraisal_source.get("measurement_definition")
        )
        appraisal_descriptor = {
            "unit": explicit_unit, "currency_basis": appraisal_currency,
            "area_basis": appraisal_area_basis,
            "measurement_definition": appraisal_measurement,
        }
        comparable_descriptor = {
            "unit": scale_unit, "currency_basis": scale_currency,
            "area_basis": scale_area_basis,
            "measurement_definition": _position_measurement_definition(scale_unit),
        }
        appraisal_value_m2 = None
        appraisal_unit = ""
        if explicit_m2 is not None and explicit_m2 > 0:
            if (
                appraisal_currency == "UF" and appraisal_area_basis
                and (not scale_area_basis or appraisal_area_basis == scale_area_basis)
                and (not scale_unit or explicit_unit == scale_unit)
                and (not scale_unit or are_market_position_scales_compatible(comparable_descriptor, appraisal_descriptor))
            ):
                appraisal_value_m2, appraisal_unit = explicit_m2, explicit_unit
        # Derivation belongs to the PDF parser and must use value and typed
        # surface extracted from that same document. Never borrow the property's
        # or comparable group's denominator for a separate appraisal source.
        target_unit = scale_unit or _position_basis_unit(
            _position_currency_basis(property_state.get("surface_ref_unit") or property_state.get("positioning_unit")),
            _position_area_basis(
                property_state.get("surface_ref_unit") or property_state.get("positioning_unit"),
                property_state.get("surface_ref_basis"),
            ),
        )
        if (
            appraisal_value_m2 is not None and appraisal_value_m2 > 0 and appraisal_unit
            and (not target_unit or appraisal_unit == target_unit)
        ):
            scale_unit = scale_unit or appraisal_unit
            scale_currency = "UF"
            scale_area_basis = appraisal_area_basis
            references.insert(0, {
                "kind": "APPRAISAL", "label": "Tasación individual",
                "value": appraisal_value_m2, "value_label": _format_position_value(appraisal_value_m2),
                "unit": appraisal_unit, "area_basis": appraisal_area_basis,
                "currency_basis": appraisal_currency,
                "measurement_definition": appraisal_measurement,
            })
            appraisal_on_scale = True
            appraisal_exclusion_reason = ""
        elif appraisal_value_m2 is not None and target_unit and appraisal_unit != target_unit:
            appraisal_exclusion_reason = "INCOMPATIBLE_AREA_BASIS"
    elif appraisal_available:
        if isinstance(appraisal_source, Mapping) and appraisal_source.get("verified") is not True:
            appraisal_exclusion_reason = "APPRAISAL_REFERENCE_NOT_VERIFIED"
        elif isinstance(appraisal_source, Mapping) and str(appraisal_source.get("property_code") or "") != str(property_code):
            appraisal_exclusion_reason = "APPRAISAL_PROPERTY_CODE_MISMATCH"
        else:
            appraisal_exclusion_reason = "NO_VERIFIED_STRUCTURED_REFERENCE"

    # Read only numeric, explicitly typed communal UF/m² fields. A prose summary
    # is never parsed into a marker.
    communal_value = None
    communal_unit = ""
    if isinstance(communal, Mapping):
        communal_metrics = communal.get("relevant_metrics") if isinstance(communal.get("relevant_metrics"), Mapping) else {}
        candidates = [
            (communal.get(key), communal.get("reference_unit") or communal.get("unit"), communal.get("area_basis") or communal.get("reference_area_basis") or communal.get("surface_basis"))
            for key in ("offer_uf_m2", "uf_m2_offer", "value_uf_m2", "reference_value_uf_m2", "reference_value", "value")
        ]
        candidates.append((communal.get("reference_value_uf_m2"), communal.get("reference_unit") or communal.get("unit"), communal.get("area_basis") or communal.get("reference_area_basis") or communal.get("surface_basis")))
        candidates.extend(
            (communal_metrics.get(key), communal.get("reference_unit") or communal.get("unit"), communal.get("area_basis") or communal.get("reference_area_basis") or communal.get("surface_basis"))
            for key in ("uf_m2_publicacion_actual", "uf_m2_arriendo_actual")
        )
        communal_measure = _position_measurement_definition(
            communal.get("reference_unit") or communal.get("unit"),
            communal.get("measurement_definition"),
        )
        for raw_value, raw_unit, raw_basis in candidates:
            number = _number_from_label(raw_value)
            currency_basis = _position_currency_basis(raw_unit, communal.get("currency_basis"))
            area_basis = _position_area_basis(raw_unit, raw_basis)
            normalized_unit = _position_basis_unit(currency_basis, area_basis)
            if number and number > 0 and currency_basis == "UF" and area_basis and normalized_unit:
                communal_value, communal_unit = number, normalized_unit
                break
    if communal_available:
        if not communal_source_unit or _position_currency_basis(communal_source_unit, communal.get("currency_basis")) != "UF":
            communal_exclusion_reason = "CURRENCY_BASIS_UNKNOWN_OR_UNSUPPORTED"
        elif not communal_source_basis and not _position_area_basis(communal_source_unit):
            communal_exclusion_reason = "AREA_BASIS_UNKNOWN"
        elif communal_unit and scale_area_basis and _position_area_basis(communal_unit) != scale_area_basis:
            communal_exclusion_reason = "INCOMPATIBLE_AREA_BASIS"
        elif position_simulation.get("communal_on_same_scale") is False:
            communal_exclusion_reason = "SOURCE_COMPATIBILITY_FAILED"
    if not scale_unit and communal_unit and scale_currency == "UF" and scale_area_basis:
        scale_unit = communal_unit
    if communal_value is not None and communal_unit and scale_unit:
        communal_descriptor = {
            "unit": communal_unit, "currency_basis": communal.get("currency_basis") or "UF",
            "area_basis": communal.get("area_basis") or communal.get("reference_area_basis") or communal.get("surface_basis"),
            "measurement_definition": communal_measure,
        }
        comparable_descriptor = {
            "unit": scale_unit, "currency_basis": scale_currency,
            "area_basis": scale_area_basis,
            "measurement_definition": _position_measurement_definition(scale_unit),
        }
        # Preserve the explicit historical control as an additional fail-closed
        # safeguard, then require all three canonical dimensions to match.
        established_compatibility = position_simulation.get("communal_on_same_scale")
        communal_compatible = (
            established_compatibility is not False
            and are_market_position_scales_compatible(comparable_descriptor, communal_descriptor)
        )
        if communal_compatible:
            references.append({
                "kind": "COMMUNAL", "label": "Oferta comunal",
                "value": communal_value, "value_label": _format_position_value(communal_value),
                "unit": communal_unit, "area_basis": _position_area_basis(communal_unit, communal_descriptor["area_basis"]),
                "currency_basis": _position_currency_basis(communal_unit, communal_descriptor["currency_basis"]),
                "measurement_definition": communal_measure,
            })
            communal_on_scale = True
            communal_exclusion_reason = ""

    # If no comparable source defines the scale, a verified explicit unit can
    # still support an appraisal/communal-only synthesis with a same-basis
    # current property value.
    if not scale_unit and references:
        scale_unit = _position_unit_label(references[0].get("unit"))
        scale_currency = _position_currency_basis(scale_unit)
        scale_area_basis = _position_area_basis(scale_unit)
    if not scale_unit or scale_currency != "UF" or not scale_area_basis or not references:
        return unavailable_result("NO_VERIFIED_MARKET_REFERENCE_ON_SCALE")
    compatible_refs = [
        ref for ref in references
        if are_market_position_scales_compatible(
            {
                "unit": scale_unit, "currency_basis": scale_currency,
                "area_basis": scale_area_basis,
                "measurement_definition": _position_measurement_definition(scale_unit),
            }, ref,
        )
    ]
    appraisal_on_scale = any(ref.get("kind") == "APPRAISAL" for ref in compatible_refs)
    communal_on_scale = any(ref.get("kind") == "COMMUNAL" for ref in compatible_refs)
    if appraisal_available and appraisal_source and not appraisal_on_scale and appraisal_exclusion_reason == "":
        appraisal_exclusion_reason = "INCOMPATIBLE_AREA_BASIS"
    if communal_available and not communal_on_scale and communal_exclusion_reason == "":
        communal_exclusion_reason = "INCOMPATIBLE_AREA_BASIS"
    if not compatible_refs:
        return unavailable_result("NO_VERIFIED_MARKET_REFERENCE_ON_SCALE")

    current_price_number = _number_from_label(current_price)
    property_unit_source = property_state.get("surface_ref_unit") or property_state.get("positioning_unit")
    # The current listing price is divided by the exact canonical comparable
    # denominator. This binds the property marker to the same scale without
    # relabelling a hybrid comparable surface as useful or built area.
    property_basis = scale_area_basis if comparable_ok and _number_from_label(position_static.get("property_value")) is not None else _position_area_basis(
        property_unit_source, property_state.get("surface_ref_basis")
    )
    property_measurement = _position_measurement_definition(
        scale_unit if comparable_ok else property_unit_source,
        property_state.get("measurement_definition"),
    )
    property_descriptor = {
        "unit": _position_basis_unit("UF", property_basis),
        "currency_basis": "UF", "area_basis": property_basis,
        "measurement_definition": property_measurement,
    }
    scale_descriptor = {
        "unit": scale_unit, "currency_basis": scale_currency,
        "area_basis": scale_area_basis,
        "measurement_definition": _position_measurement_definition(scale_unit),
    }
    current_value = None
    denominator = comparable_surface or property_surface
    denominator_matches = bool(
        property_surface and property_surface > 0 and denominator and denominator > 0
        and (
            comparable_surface is None
            or abs(comparable_surface - property_surface) <= max(0.01, comparable_surface * 0.001)
        )
    )
    if (
        current_price_number and current_price_number > 0 and denominator_matches
        and are_market_position_scales_compatible(scale_descriptor, property_descriptor)
    ):
        current_value = current_price_number / denominator
        canonical_property_value = _number_from_label(position_static.get("property_value"))
        if canonical_property_value is not None and abs(current_value - canonical_property_value) > max(0.11, current_value * 0.001):
            return unavailable_result("POSITIONING_SURFACE_DENOMINATOR_MISMATCH")
    elif comparable_ok and are_market_position_scales_compatible(scale_descriptor, property_descriptor):
        # A frozen canonical comparable snapshot may contain a verified
        # property UF/m² pair even when the surface denominator needed for a
        # proposed-price simulation is unavailable. Keep that static point.
        current_value = _number_from_label(position_static.get("property_value"))
    if current_value is None or current_value <= 0:
        return unavailable_result("PROPERTY_VALUE_NOT_VERIFIABLE_ON_SCALE")

    comparable_value = next((ref["value"] for ref in compatible_refs if ref["kind"] == "COMPARABLE"), None)
    appraisal_value = next((ref["value"] for ref in compatible_refs if ref["kind"] == "APPRAISAL"), None)
    communal_value_on_scale = next((ref["value"] for ref in compatible_refs if ref["kind"] == "COMMUNAL"), None)
    primary_kind = "COMPARABLE" if comparable_value is not None else "APPRAISAL" if appraisal_value is not None else "COMMUNAL"

    def gap_for(value: float, reference: float) -> float:
        return (value / reference - 1) * 100

    def gap_text(value: float, reference: float, kind: str) -> str:
        pct = round(gap_for(value, reference), 1)
        sign = "+" if pct > 0 else "" if pct == 0 else "-"
        label = {"APPRAISAL": "tasación", "COMPARABLE": "similares", "COMMUNAL": "oferta comunal"}.get(kind, "referencia")
        return f"{sign}{_format_position_value(abs(pct))}% vs {label}"

    def copy_gap(value: float, reference: float, kind: str) -> str:
        pct = round(gap_for(value, reference), 1)
        label = {
            "APPRAISAL": "la tasación",
            "COMPARABLE": "las propiedades similares",
            "COMMUNAL": "la oferta comunal",
        }.get(kind, "la referencia")
        direction = "por sobre" if pct > 0 else "por debajo de"
        if abs(pct) <= 3.0:
            if abs(pct) < 0.05:
                return f"en línea con {label}"
            neutral_direction = "por sobre" if pct > 0 else "por debajo"
            return f"en línea con {label} ({_format_position_value(abs(pct))}% {neutral_direction})"
        return f"{_format_position_value(abs(pct))}% {direction} {label}"

    def join_copy_gaps(value: float) -> str:
        clauses = [copy_gap(value, ref["value"], ref["kind"]) for ref in compatible_refs]
        return clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + " y " + clauses[-1]

    def comparable_reference_phrase() -> str:
        count = _number_from_label(position_static.get("count"))
        if count is not None and count > 0:
            count_int = int(count)
            return "la referencia de 1 propiedad similar" if count_int == 1 else f"la referencia de {count_int} propiedades similares"
        return "la referencia de las propiedades similares seleccionadas"

    def recommended_comparable_status(value: float) -> str:
        if not compatible_refs or compatible_refs[0]["kind"] != "COMPARABLE":
            return ""
        recommended_gap = gap_for(value, compatible_refs[0]["value"])
        if recommended_gap > 3.0:
            return "aunque la propiedad seguiría por sobre esa referencia"
        if recommended_gap < -3.0:
            return "y quedaría por debajo de esa referencia"
        return "acercándose al rango de referencia"

    def single_comparable_copy(value: float, *, recommended: bool = False) -> str:
        ref = compatible_refs[0]
        pct = round(gap_for(value, ref["value"]), 1)
        reference_phrase = comparable_reference_phrase()
        value_label = f"{_format_position_value(value)} {owner_unit}"
        current_pct = round(gap_for(current_value, ref["value"]), 1)
        if recommended:
            if abs(pct) <= 3.0:
                outcome = f"la propiedad se situaría {copy_gap(value, ref['value'], ref['kind'])}"
            else:
                outcome = f"la propiedad se situaría un {copy_gap(value, ref['value'], ref['kind'])}"
            if abs(pct) < abs(current_pct) - 0.05:
                change = "reduciendo la diferencia respecto del precio actual"
            elif abs(pct) > abs(current_pct) + 0.05:
                change = "ampliando la diferencia respecto del precio actual"
            else:
                change = "manteniendo una diferencia similar respecto del precio actual"
            status = recommended_comparable_status(value)
            suffix = f", {status}" if status else ""
            return f"Con el precio recomendado de {value_label}, {outcome}, {change}{suffix}."

        if abs(pct) <= 3.0:
            first = f"Tu propiedad se publica en {value_label}, en línea con {reference_phrase}."
        else:
            first = f"Tu propiedad se publica en {value_label}, un {_format_position_value(abs(pct))}% {'por sobre' if pct > 0 else 'por debajo de'} {reference_phrase}."
        if proposed_value is None or proposed_value <= 0:
            return first
        proposed_pct = round(gap_for(proposed_value, ref["value"]), 1)
        recommended_label = f"{_format_position_value(proposed_value)} {owner_unit}"
        if abs(proposed_pct) <= 3.0:
            second = f"Con el precio recomendado de {recommended_label}, la propiedad quedaría en línea con esa referencia, acercándose al rango de referencia."
        else:
            movement = "se reduciría" if abs(proposed_pct) < abs(pct) - 0.05 else "aumentaría" if abs(proposed_pct) > abs(pct) + 0.05 else "se mantendría"
            status = recommended_comparable_status(proposed_value)
            suffix = f", {status}" if status else ""
            second = f"Con el precio recomendado de {recommended_label}, la diferencia {movement} a {_format_position_value(abs(proposed_pct))}%{suffix}."
        return f"{first} {second}"

    def copy_for(value: float, adjusted: bool = False) -> str:
        if not compatible_refs:
            return ""
        if len(compatible_refs) == 1 and compatible_refs[0]["kind"] == "COMPARABLE":
            return single_comparable_copy(value, recommended=adjusted)
        value_label = f"{_format_position_value(value)} {owner_unit}"
        joined = join_copy_gaps(value)
        if adjusted:
            return f"Con el precio recomendado de {value_label}, la propiedad se situaría {joined}."
        current_sentence = f"Actualmente, tu propiedad se publica en {value_label}: {joined}."
        if proposed_value is None or proposed_value <= 0:
            return current_sentence
        proposed_label = f"{_format_position_value(proposed_value)} {owner_unit}"
        proposed_gaps = join_copy_gaps(proposed_value)
        return f"{current_sentence} Con el precio recomendado de {proposed_label}, se situaría {proposed_gaps}."

    simulation_available = bool(position_simulation.get("available"))
    proposed_value = None
    if recommendation_is_monthly:
        proposed_price_number = _number_from_label(recommended_price)
        if (
            proposed_price_number and proposed_price_number > 0 and current_price_number and current_price_number > 0
            and denominator_matches and are_market_position_scales_compatible(scale_descriptor, property_descriptor)
        ):
            proposed_value = proposed_price_number / denominator
            frozen_simulated_value = _number_from_label(position_simulation.get("proposed_m2"))
            if frozen_simulated_value is not None and abs(proposed_value - frozen_simulated_value) > max(0.11, proposed_value * 0.001):
                proposed_value = None
            else:
                simulation_available = True
    if proposed_value is None or proposed_value <= 0:
        simulation_available = False

    # Bracket and headline focus on the primary reference; every available gap
    # remains explicit in the changing text and accessible live label.
    primary_reference = next(ref for ref in compatible_refs if ref["kind"] == primary_kind)
    current_gap = gap_for(current_value, primary_reference["value"])
    recommended_available = proposed_value is not None and proposed_value > 0
    proposed_gap = gap_for(proposed_value, primary_reference["value"]) if recommended_available else None
    domain_values = [current_value] + [ref["value"] for ref in compatible_refs]
    if recommended_available and proposed_value is not None:
        domain_values.append(proposed_value)
    domain = _position_scale(domain_values)
    if domain is None:
        return unavailable_result("POSITIONING_SCALE_UNAVAILABLE")
    property_x = _position_scale_pct(current_value, domain)
    primary_x = _position_scale_pct(primary_reference["value"], domain)
    for ref in compatible_refs:
        ref["x"] = _position_scale_pct(ref["value"], domain)
        ref["gap_pct"] = gap_for(current_value, ref["value"])
        ref["gap_label"] = gap_text(current_value, ref["value"], ref["kind"])
    for ref in compatible_refs:
        ref["current_gap_label"] = gap_text(current_value, ref["value"], ref["kind"])
        ref["proposed_gap_label"] = gap_text(proposed_value, ref["value"], ref["kind"]) if recommended_available and proposed_value is not None else ""
    current_gap_label = next(ref["current_gap_label"] for ref in compatible_refs if ref["kind"] == primary_kind)
    adjusted_gap_label = next(ref["proposed_gap_label"] for ref in compatible_refs if ref["kind"] == primary_kind) if recommended_available else ""

    source_labels = []
    if appraisal_available and isinstance(appraisal_card, Mapping):
        appraisal_source = appraisal_card.get("market_position_reference")
        appraisal_date = (
            _text(appraisal_source.get("source_date_label") or "")
            if isinstance(appraisal_source, Mapping) else ""
        ) or _text(appraisal_card.get("source_date_label"))
        if appraisal_date:
            appraisal_date = appraisal_date.replace("-", "/")
        source_labels.append("Tasación" + (f" · {appraisal_date}" if appraisal_date else ""))
    if comparable_value is not None:
        comparable_source_date = _source_date_label(comparables.get("source_date") or comparables.get("cutoff_date"))
        if comparable_source_date:
            comparable_source_date = comparable_source_date.replace("-", "/")
        count = _number_from_label(position_static.get("count"))
        comparable_label = (
            f"{int(count)} propiedades similares" if count is not None and count > 0
            else "Propiedades similares"
        )
        if comparable_source_date:
            comparable_label += f" · corte {comparable_source_date}"
        source_labels.append(comparable_label)
    if communal_available and isinstance(communal, Mapping):
        communal_date = _source_date_label(communal.get("source_date") or communal.get("cutoff_date")) if isinstance(communal, Mapping) else ""
        if communal_date:
            communal_date = communal_date.replace("-", "/")
        source_labels.append("Oferta comunal" + (f" · corte {communal_date}" if communal_date else ""))

    def classify(value: float) -> dict[str, Any]:
        categories = [
            "INLINE" if abs(gap_for(value, ref["value"])) <= 3.0
            else "ABOVE" if gap_for(value, ref["value"]) > 3.0 else "BELOW"
            for ref in compatible_refs
        ]
        low, high = min(ref["value"] for ref in compatible_refs), max(ref["value"] for ref in compatible_refs)
        if value > high and all(category == "ABOVE" for category in categories):
            label = f"Sobre {len(categories)} de {len(categories)} referencias"
            code = "ABOVE_ALL_REFERENCES"
        elif value < low and all(category == "BELOW" for category in categories):
            label = f"Bajo {len(categories)} de {len(categories)} referencias"
            code = "BELOW_ALL_REFERENCES"
        elif low <= value <= high:
            label, code = "Dentro del rango", "WITHIN_REFERENCE_RANGE"
        elif "INLINE" in categories:
            label = f"En línea con {categories.count('INLINE')} de {len(categories)} referencias"
            code = "INLINE_WITH_REFERENCES"
        else:
            label, code = "Entre referencias", "MIXED_REFERENCE_POSITION"
        if len(compatible_refs) == 1 and compatible_refs[0]["kind"] == "COMPARABLE":
            only_ref = compatible_refs[0]
            only_gap = gap_for(value, only_ref["value"])
            label = f"{gap_text(value, only_ref['value'], 'COMPARABLE')}"
            code = "SINGLE_COMPARABLE_REFERENCE"
        return {"code": code, "label": label, "above": categories.count("ABOVE"),
                "inline": categories.count("INLINE"), "below": categories.count("BELOW"),
                "count": len(categories)}

    current_classification = classify(current_value)
    proposed_classification = classify(proposed_value) if recommended_available and proposed_value is not None else None
    summary_label = current_classification["label"]
    if len(compatible_refs) == 1 and compatible_refs[0]["kind"] == "COMPARABLE":
        only_gap = round(gap_for(current_value, compatible_refs[0]["value"]), 1)
        sign = "+" if only_gap > 0 else "" if only_gap == 0 else "-"
        summary_label = f"{sign}{_format_position_value(abs(only_gap))}% vs propiedades similares"
    summary_source_names = {
        "APPRAISAL": "Tasación", "COMPARABLE": "similares", "COMMUNAL": "oferta comunal",
    }
    summary_sources = " · ".join(summary_source_names[ref["kind"]] for ref in compatible_refs)
    extra_reference_count = int(appraisal_available and not appraisal_on_scale) + int(communal_available and not communal_on_scale)
    if len(compatible_refs) == 1 and compatible_refs[0]["kind"] == "COMPARABLE":
        summary_note = "1 referencia disponible"
        if extra_reference_count:
            noun = "referencia adicional" if extra_reference_count == 1 else "referencias adicionales"
            summary_note += f" · {extra_reference_count} {noun} disponible" + ("s" if extra_reference_count != 1 else "")
    else:
        summary_note = f"{len(compatible_refs)} referencias: {summary_sources}"
    refs_by_kind = {ref["kind"]: ref for ref in compatible_refs}
    current_gaps = [
        {"kind": ref["kind"], "label": ref["label"], "text": ref["current_gap_label"], "is_primary": ref["kind"] == primary_kind}
        for ref in compatible_refs
    ]
    proposed_gaps = [
        {"kind": ref["kind"], "label": ref["label"], "text": ref["proposed_gap_label"], "is_primary": ref["kind"] == primary_kind}
        for ref in compatible_refs if ref.get("proposed_gap_label")
    ]
    bar_markers = []
    for kind in ("APPRAISAL", "COMPARABLE", "COMMUNAL"):
        ref = refs_by_kind.get(kind)
        if ref:
            bar_markers.append({**ref, "unit": owner_unit, "marker_type": "REFERENCE"})
    if recommended_available and proposed_value is not None:
        bar_markers.append({
            "kind": "PROPERTY_RECOMMENDED", "label": "Precio recomendado",
            "value": proposed_value, "value_label": _format_position_value(proposed_value),
            "unit": owner_unit, "area_basis": scale_area_basis,
            "currency_basis": scale_currency, "measurement_definition": scale_descriptor["measurement_definition"],
            "x": _position_scale_pct(proposed_value, domain), "marker_type": "PROPERTY",
            "gaps": proposed_gaps,
        })
    bar_markers.append({
        "kind": "PROPERTY_CURRENT", "label": "Precio actual",
        "value": current_value, "value_label": _format_position_value(current_value),
        "unit": owner_unit, "area_basis": scale_area_basis,
        "currency_basis": scale_currency, "measurement_definition": scale_descriptor["measurement_definition"],
        "x": property_x, "marker_type": "PROPERTY", "gaps": current_gaps,
    })
    return {
        "available": True, "simulation_available": simulation_available,
        "count": position_static.get("count") if comparable_ok else None,
        "unit": owner_unit, "references": compatible_refs, "bar_markers": bar_markers,
        "scale": {
            "currency": scale_currency, "denominator_basis": scale_area_basis,
            "unit_label": scale_unit,
            "measurement_definition": scale_descriptor["measurement_definition"],
        },
        "property": {
            "current_uf_m2": current_value,
            "recommended_uf_m2": proposed_value if recommended_available else None,
            "area_basis": scale_area_basis,
            "surface_ref_m2": denominator,
            "currency_basis": scale_currency,
            "measurement_definition": scale_descriptor["measurement_definition"],
        },
        "sources_label": ("Fuentes: " + " · ".join(source_labels)) if source_labels else "",
        "current_value": current_value, "current_value_label": _format_position_value(current_value),
        "current_x": property_x,
        "proposed_value": proposed_value if recommended_available else None,
        "proposed_value_label": _format_position_value(proposed_value) if recommended_available else "",
        "proposed_x": _position_scale_pct(proposed_value, domain) if recommended_available and proposed_value is not None else None,
        "primary_kind": primary_kind,
        "primary_x": primary_x,
        "gap_left": min(primary_x, property_x), "gap_width": abs(property_x - primary_x),
        "current_gap_label": current_gap_label,
        "proposed_gap_label": adjusted_gap_label,
        "current_classification": current_classification,
        "proposed_classification": proposed_classification,
        "summary_position_label": summary_label,
        "summary_position_note": summary_note or "Sin referencias comparables",
        **diagnostics(),
        "current_copy": copy_for(current_value),
        "adjusted_copy": copy_for(proposed_value, adjusted=True) if recommended_available and proposed_value is not None else "",
        "adjustment_label": position_simulation.get("adjustment_label") or "",
        "property_current_gaps": current_gaps,
        "property_recommended_gaps": proposed_gaps,
    }


def _market_evidence_model(
    *, market_position: Mapping[str, Any], position_static: Mapping[str, Any],
    appraisal_card: Mapping[str, Any] | None, communal: Mapping[str, Any],
    stale: bool,
) -> dict[str, Any]:
    """Keep all verified market sources distinct from the geometric scale subset."""
    sources: list[dict[str, Any]] = []
    if not stale and isinstance(appraisal_card, Mapping):
        app_ref = appraisal_card.get("market_position_reference")
        app_ref = app_ref if isinstance(app_ref, Mapping) else {}
        appraisal_value = _number_from_label(app_ref.get("appraisal_uf_m2"))
        appraisal_basis = _text(app_ref.get("area_basis"))
        appraisal_unit = _text(app_ref.get("appraisal_uf_m2_unit"))
        if appraisal_value is not None and not appraisal_unit:
            appraisal_unit = _position_basis_unit("UF", appraisal_basis) if appraisal_basis else "UF/m² · superficie no informada"
        elif appraisal_value is not None and not appraisal_basis and not re.search(r"útil|util|built|construid|terreno|land", appraisal_unit, re.I):
            appraisal_unit = f"{appraisal_unit or 'UF/m²'} · superficie no informada"
        sources.append({
            "kind": "APPRAISAL", "label": "Tasación individual", "owner_label": "Tasación",
            "available": True, "structured": appraisal_card.get("mode") == "STRUCTURED",
            "value": appraisal_value,
            # A DOCUMENT_ONLY PDF is a source, not a numeric market reference.
            "quantitative_value": appraisal_value,
            "value_label": f"{_format_position_value(appraisal_value)} {appraisal_unit}" if appraisal_value is not None else "",
            "unit": appraisal_unit,
            "area_basis": appraisal_basis,
            "currency_basis": _position_currency_basis(appraisal_unit, app_ref.get("currency_basis")),
            "measurement_definition": _position_measurement_definition(
                appraisal_unit, app_ref.get("measurement_definition")
            ),
            "source": "INDIVIDUAL_APPRAISAL",
        })
    if not stale and isinstance(position_static, Mapping) and position_static.get("available"):
        sources.append({
            "kind": "COMPARABLE", "label": "Propiedades similares", "owner_label": "Similares",
            "available": True, "structured": True,
            "value": _number_from_label(position_static.get("reference_value")),
            "quantitative_value": _number_from_label(position_static.get("reference_value")),
            "unit": _position_unit_label(position_static.get("unit")),
            "value_label": (
                f"{_format_position_value(_number_from_label(position_static.get('reference_value')))} {_position_unit_label(position_static.get('unit'))}"
                if _number_from_label(position_static.get("reference_value")) is not None else ""
            ),
            "area_basis": _position_area_basis(position_static.get("unit")),
            "currency_basis": _position_currency_basis(position_static.get("unit")),
            "measurement_definition": _position_measurement_definition(position_static.get("unit")),
            "count": position_static.get("count"),
            "source": _text(position_static.get("source") or "VERIFIED_COMPARABLES"),
        })
    communal_metrics = communal.get("relevant_metrics") if isinstance(communal.get("relevant_metrics"), Mapping) else {}
    communal_value = None
    for communal_key in ("reference_value_uf_m2", "offer_uf_m2", "uf_m2_offer", "uf_m2_publicacion_actual"):
        communal_value = _number_from_label(communal.get(communal_key))
        if communal_value is not None:
            break
    if communal_value is None:
        communal_value = _number_from_label(communal_metrics.get("uf_m2_publicacion_actual"))
    if not stale and communal_value is not None and communal_value > 0:
        unit = _text(communal.get("reference_unit") or communal.get("unit"))
        basis = _text(communal.get("area_basis") or communal.get("reference_area_basis") or communal.get("surface_basis"))
        communal_unit = _text(communal.get("reference_unit") or communal.get("unit"))
        communal_display_unit = (
            _position_basis_unit("UF", basis)
            if _position_area_basis(communal_unit, basis)
            else "UF/m²"
        )
        sources.append({
            "kind": "COMMUNAL", "label": "Oferta comunal", "owner_label": "Oferta comunal",
            "available": True, "structured": True,
            "value": communal_value,
            "quantitative_value": communal_value,
            "value_label": f"{_format_position_value(communal_value)} {communal_display_unit}" if communal_value is not None else "",
            "unit": communal_unit or unit, "area_basis": basis,
            "currency_basis": _position_currency_basis(communal_unit or unit, communal.get("currency_basis")),
            "measurement_definition": _position_measurement_definition(
                communal_unit or unit, communal.get("measurement_definition")
            ),
            "source": "mercado_comunal", "source_date": communal.get("source_date"),
        })
    compatible_by_kind = {
        str(ref.get("kind")): dict(ref)
        for ref in (market_position.get("references") or [])
        if isinstance(ref, Mapping)
    }
    compatible_kinds = set(compatible_by_kind)
    sources = [
        {**source, "scale_compatible": source["kind"] in compatible_kinds}
        for source in sources
    ]
    compatible = [
        {**source, "scale_reference": compatible_by_kind[source["kind"]]}
        for source in sources if source["kind"] in compatible_by_kind
    ]
    labels = [source["owner_label"] for source in sources]
    compatible_count = len(compatible)
    quantitative = [source for source in sources if source.get("value") is not None or source.get("quantitative_value") is not None]
    summary_label = (
        f"{len(sources)} fuente disponible" if len(sources) == 1
        else f"{len(sources)} fuentes disponibles" if sources else "—"
    )
    current_value = _number_from_label(market_position.get("current_value"))
    if sources and compatible_count == len(sources) and current_value is not None:
        comparable_values = [
            _number_from_label(item["scale_reference"].get("value"))
            for item in compatible
        ]
        if comparable_values and all(value is not None and current_value > value for value in comparable_values):
            summary_label = f"Sobre {compatible_count} referencias comparables"
    if compatible_count:
        compat_note = f"{compatible_count} referencia" + ("s" if compatible_count != 1 else "") + " directamente en la misma escala"
    else:
        compat_note = "Sin referencias con unidad y superficie verificadas en una misma escala"
    quantitative_label = "referencia con valor" if len(quantitative) == 1 else "referencias con valor"
    quantitative_note = f"{len(quantitative)} {quantitative_label}"
    summary_note = " · ".join([quantitative_note, compat_note, " · ".join(labels)] if labels else [quantitative_note, compat_note])
    return {
        "available": bool(sources), "sources": sources,
        "scale_compatible_sources": compatible,
        "evidence_source_count": len(sources),
        "quantitative_reference_count": len(quantitative),
        "scale_compatible_count": compatible_count,
        "summary_position_label": summary_label,
        "summary_position_note": summary_note,
    }


def _position_data(evidence: Mapping[str, Any], monthly: Mapping[str, Any]) -> dict[str, Any]:
    comparable = monthly.get("comparables") if isinstance(monthly.get("comparables"), Mapping) else {}
    owner_unit = _comparable_owner_unit_label(comparable)
    labels = evidence.get("position-anchor-label", [])
    labels = labels if isinstance(labels, list) else []
    reference_boxes = evidence.get("position-ref-box", [])
    property_boxes = evidence.get("position-prop-box", [])
    email_reference = reference_boxes[0] if reference_boxes else (labels[0] if labels else "")
    email_subject = property_boxes[0] if property_boxes else (labels[1] if len(labels) > 1 else "")
    monthly_reference = _text(comparable.get("reference_value"))
    monthly_subject = _text(comparable.get("property_value"))
    monthly_unit = _text(comparable.get("unit"))
    monthly_pair_valid = (
        _number_from_label(monthly_reference) is not None
        and _number_from_label(monthly_subject) is not None
        and bool(monthly_unit or (_position_unit_label(monthly_reference) and
                                  _position_unit_label(monthly_reference) == _position_unit_label(monthly_subject)))
    )
    if monthly_pair_valid:
        reference, subject, selected_source = monthly_reference, monthly_subject, "MONTHLY_COMPARABLES"
        if isinstance(comparable.get("reference_value"), (int, float)):
            reference = f"{comparable['reference_value']:g}".replace(".", ",") + (f" {monthly_unit}" if monthly_unit else "")
        if isinstance(comparable.get("property_value"), (int, float)):
            subject = f"{comparable['property_value']:g}".replace(".", ",") + (f" {monthly_unit}" if monthly_unit else "")
    elif _number_from_label(email_reference) is not None and _number_from_label(email_subject) is not None:
        # Historical values stay paired to the same immutable sent-email source;
        # do not fill one half of that pair from a different snapshot.
        reference, subject, selected_source = email_reference, email_subject, "VERIFIED_SENT_EMAIL"
    else:
        reference = monthly_reference or email_reference
        subject = monthly_subject or email_subject
        selected_source = "PARTIAL"
    reference = re.sub(r"^Referencia de mercado\s*", "", reference, flags=re.IGNORECASE)
    subject = re.sub(r"^Tu propiedad\s*", "", subject, flags=re.IGNORECASE)
    count = comparable.get("count") if selected_source == "MONTHLY_COMPARABLES" else None
    if count is None:
        count = (evidence.get("evidence-badge-single") or [None])[0]
    if isinstance(count, str):
        count = int(_number_from_label(count)) if _number_from_label(count) is not None else count
    ref_number = _number_from_label(reference)
    subject_number = _number_from_label(subject)
    reference_unit = _position_unit_label(reference)
    subject_unit = _position_unit_label(subject)
    unit = monthly_unit if selected_source == "MONTHLY_COMPARABLES" else (
        reference_unit if reference_unit and reference_unit == subject_unit else ""
    )
    if not owner_unit:
        owner_unit = _position_unit_label(unit or reference or subject) or "UF/m²"
    marker_pct = None
    gap_pct = _position_gap(reference, subject, unit)
    if gap_pct is not None:
        # Display-only chart coordinate; no recommendation/pricing is derived here.
        marker_pct = max(4.0, min(96.0, 50.0 + gap_pct * 1.25))
    return {
        "count": count,
        "reference_value": reference or None,
        "property_value": subject or None,
        "unit": unit,
        "owner_unit": owner_unit,
        "source": selected_source,
        "interpretation": _text(comparable.get("positioning")) or (evidence.get("comparable-summary-single") or [None])[0],
        "marker_pct": marker_pct,
        "marker_label_pct": max(25.0, min(75.0, marker_pct)) if marker_pct is not None else None,
        "gap_pct": gap_pct,
        "gap_label": f"{gap_pct:+.0f}%" if gap_pct is not None else "",
        "gap_note": "sobre referencia" if gap_pct is not None and gap_pct > 0 else "bajo referencia" if gap_pct is not None and gap_pct < 0 else "en referencia",
    }


def resolve_market_area_basis(source: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve area semantics globally from typed analysis fields, never values or identity."""
    analysis = source.get("analisis_comparables") if isinstance(source.get("analisis_comparables"), Mapping) else source
    market = analysis.get("mercado") if isinstance(analysis.get("mercado"), Mapping) else {}
    client = analysis.get("client_evidence") if isinstance(analysis.get("client_evidence"), Mapping) else {}
    explicit_raw = (
        source.get("area_basis") or source.get("reference_area_basis") or source.get("surface_basis")
        or market.get("area_basis") or market.get("reference_area_basis") or market.get("surface_basis")
    )
    explicit_basis = _position_area_basis("", _text(explicit_raw))
    price_surface = _text(source.get("price_surface") or market.get("price_surface")).casefold().strip()
    indicator = _text(source.get("primary_indicator") or client.get("primary_indicator"))
    indicator_unit = _position_unit_label(indicator)
    indicator_basis = _position_area_basis(indicator_unit)
    currency_basis = (
        _position_currency_basis(indicator)
        or _position_currency_basis(source.get("unit") or market.get("unit"))
        or "UF"
    )

    if explicit_basis:
        return {
            "basis": explicit_basis,
            "label": _position_basis_unit(currency_basis, explicit_basis),
            "source": "area_basis",
            "verified": True,
        }

    surface_aliases = {
        "land_m2": "LAND", "superficie_terreno": "LAND", "terreno_m2": "LAND",
        "built_m2": "BUILT", "superficie_construida": "BUILT", "constructed_m2": "BUILT",
        "useful_m2": "USEFUL", "surface_util_m2": "USEFUL", "superficie_util": "USEFUL",
    }
    surface_basis = surface_aliases.get(price_surface)
    expected_basis = surface_basis
    source_label = ""
    if surface_basis:
        if indicator_basis and indicator_basis != surface_basis:
            return {"basis": "UNKNOWN", "label": "UF/m²", "source": "CONFLICTING_AREA_SEMANTICS", "verified": False}
        source_label = "analisis_comparables.mercado.price_surface"
    elif price_surface in {"surface_ref_m2", ""} and indicator_basis:
        # surface_ref_m2 is a modeled denominator selected from the available
        # useful/built data. Its canonical indicator preserves that distinction.
        expected_basis = indicator_basis
        source_label = "analisis_comparables.client_evidence.primary_indicator"
    elif not price_surface and explicit_basis:
        expected_basis = explicit_basis
        source_label = "area_basis"
    elif not price_surface and indicator_basis:
        expected_basis = indicator_basis
        source_label = "analisis_comparables.client_evidence.primary_indicator"

    if not expected_basis:
        return {"basis": "UNKNOWN", "label": f"{currency_basis}/m²", "source": source_label or "AREA_BASIS_NOT_VERIFIED", "verified": False}
    return {
        "basis": expected_basis,
        "label": _position_basis_unit(currency_basis, expected_basis),
        "source": source_label,
        "verified": True,
    }


def _comparable_owner_unit_label(comparables: Mapping[str, Any]) -> str:
    return resolve_market_area_basis(comparables)["label"]


def _static_position_data(
    *, position: Mapping[str, Any], comparables: Mapping[str, Any],
    historical_email_verified: bool, campaign_comparable_mode: str, stale: bool,
) -> dict[str, Any]:
    """Expose validated current comparable evidence without requiring a price simulation."""
    unavailable = {"available": False, "reason": "COMPARABLE_EVIDENCE_INSUFFICIENT"}
    if stale:
        return {**unavailable, "reason": "STALE_PORTAL"}
    if not isinstance(comparables, Mapping) or comparables.get("evidence_conflict") is True:
        return {**unavailable, "reason": "COMPARABLE_EVIDENCE_CONFLICT"}
    if str(comparables.get("display_status") or "OK").upper() == "REVIEW":
        return {**unavailable, "reason": "COMPARABLE_EVIDENCE_CONFLICT"}

    source = str(position.get("source") or "").upper()
    if source == "MONTHLY_COMPARABLES":
        evidence_level = str(comparables.get("evidence_level") or "").upper()
        version = _text(comparables.get("version") or comparables.get("algorithm_version"))
        if evidence_level not in {"HIGH", "MEDIUM"} or (version and "cluster_v2" not in version.casefold()):
            return {**unavailable, "reason": "COMPARABLE_SOURCE_NOT_CANONICAL"}
        if str(comparables.get("positioning_mode") or "PRICE_M2").upper() != "PRICE_M2":
            return {**unavailable, "reason": "COMPARABLE_POSITIONING_MODE_UNSUPPORTED"}
        if str(comparables.get("comparable_mode") or "").upper() == "COMMUNAL_FALLBACK":
            return {**unavailable, "reason": "NON_PRIMARY_COMPARABLE_MODE"}
        count = _number_from_label(comparables.get("count") or comparables.get("selected_n"))
    elif source == "VERIFIED_SENT_EMAIL":
        if not historical_email_verified or campaign_comparable_mode != "PRIMARY_COMPARABLES":
            return {**unavailable, "reason": "NON_PRIMARY_COMPARABLE_MODE"}
        # Count and both displayed values are taken together from the same
        # identity-verified immutable email; campaign totals are not mixed in.
        count = _number_from_label(position.get("count"))
    else:
        return {**unavailable, "reason": "COMPARABLE_SOURCE_NOT_CANONICAL"}

    reference = position.get("reference_value")
    property_value = position.get("property_value")
    reference_number = _number_from_label(reference)
    property_number = _number_from_label(property_value)
    reference_unit = _position_unit_label(reference)
    property_unit = _position_unit_label(property_value)
    unit = reference_unit or property_unit or _text(position.get("unit"))
    if count is None or count < 5:
        return {**unavailable, "reason": "COMPARABLE_SAMPLE_TOO_SMALL"}
    if reference_number is None or reference_number <= 0 or property_number is None or property_number <= 0:
        return {**unavailable, "reason": "COMPARABLE_VALUES_MISSING"}
    if not unit or not reference_unit or reference_unit != property_unit:
        return {**unavailable, "reason": "COMPARABLE_UNITS_INCOMPATIBLE"}
    gap = _position_gap(reference, property_value)
    if gap is None:
        return {**unavailable, "reason": "COMPARABLE_GAP_NOT_VERIFIABLE"}

    rounded_gap = round(gap, 1)
    gap_number = _format_position_value(abs(rounded_gap))
    if rounded_gap > 0:
        gap_label = f"+{gap_number}% sobre propiedades similares"
    elif rounded_gap < 0:
        gap_label = f"-{gap_number}% bajo propiedades similares"
    else:
        gap_label = f"{gap_number}% en línea con propiedades similares"
    return {
        "available": True,
        "count": int(count),
        "source": source,
        "owner_unit": _text(position.get("owner_unit")) or unit,
        "reference_value": reference,
        "property_value": property_value,
        "reference_label": f"{_format_position_value(reference_number)} {_text(position.get('owner_unit')) or unit}",
        "property_label": f"{_format_position_value(property_number)} {_text(position.get('owner_unit')) or unit}",
        "unit": unit,
        "gap_pct": gap,
        "gap_label": gap_label,
    }


_MARKET_CONTEXT_KIND_ALIASES = {
    "MORTGAGE_REFERENCE": {"MORTGAGE_REFERENCE", "MORTGAGE_RATE", "TASA_HIPOTECARIA"},
    "UNEMPLOYMENT": {"UNEMPLOYMENT", "UNEMPLOYMENT_RATE", "DESEMPLEO"},
    "TPM": {"TPM", "MONETARY_POLICY_RATE"},
    "REGIONAL_HOME_SALES": {"REGIONAL_HOME_SALES", "HOME_SALES", "HOUSING_SALES"},
    "REAL_WAGES": {"REAL_WAGES", "REAL_WAGE_CHANGE", "REMUNERACIONES_REALES"},
    "CPI": {"CPI", "IPC", "CONSUMER_PRICE_INDEX"},
    "REGIONAL_RENTAL_INDICATOR": {"REGIONAL_RENTAL_INDICATOR", "RENTAL_MARKET", "MERCADO_ARRIENDO"},
    "NEW_HOUSING_COMPETITION": {"NEW_HOUSING_COMPETITION", "FOGAES", "FOGAES_CONTEXT"},
}
_MARKET_CONTEXT_LABELS = {
    "MORTGAGE_REFERENCE": "Tasa hipotecaria de referencia", "UNEMPLOYMENT": "Desempleo",
    "TPM": "TPM", "REGIONAL_HOME_SALES": "Ventas inmobiliarias",
    "REAL_WAGES": "Remuneraciones reales", "CPI": "IPC",
    "REGIONAL_RENTAL_INDICATOR": "Mercado de arriendo",
    "NEW_HOUSING_COMPETITION": "Competencia de vivienda nueva",
}
def _market_context_fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _text(value).casefold())
    return "".join(char for char in text if not unicodedata.combining(char))


def _market_context_region_key(value: Any) -> str:
    folded = re.sub(r"\s+", " ", _market_context_fold(value)).strip()
    folded = re.sub(r"^(?:region|reg\.?)(?:\s+de)?\s+", "", folded)
    region_codes = {
        "rm": "metropolitana", "vs": "valparaiso", "an": "antofagasta",
        "at": "atacama", "co": "coquimbo", "li": "ohiggins", "ml": "maule",
        "nb": "nuble", "bi": "biobio", "ar": "araucania", "lr": "rios",
        "ll": "lagos", "ai": "aysen", "ma": "magallanes", "ap": "arica y parinacota",
        "ta": "tarapaca",
    }
    if folded in region_codes:
        return region_codes[folded]
    if folded in {"bio-bio", "bio bio", "biobio"}:
        return "biobio"
    if folded in {"metropolitana de santiago", "metropolitana"}:
        return "metropolitana"
    if folded in {"bernardo ohiggins", "libertador bernardo ohiggins", "libertador general bernardo ohiggins", "o higgins", "ohiggins"}:
        return "ohiggins"
    if folded in {"los rios", "rios"}:
        return "rios"
    if folded in {"los lagos", "lagos"}:
        return "lagos"
    if folded in {"region de arica y parinacota", "arica y parinacota"}:
        return "arica y parinacota"
    return folded


def _market_context_operation(value: Any) -> str:
    folded = _market_context_fold(value)
    if folded in {"venta", "sell", "sale"}:
        return "VENTA"
    if folded in {"arriendo", "rent", "rental", "alquiler"}:
        return "ARRIENDO"
    return ""


def _market_context_kind(value: Any) -> str:
    key = re.sub(r"[^A-Z0-9]+", "_", _market_context_fold(value).upper()).strip("_")
    return next((kind for kind, aliases in _MARKET_CONTEXT_KIND_ALIASES.items() if key in aliases), "")


def _market_context_geo(indicator: Mapping[str, Any], *, region: str, commune: str) -> tuple[bool, str, int]:
    level = re.sub(r"[^A-Z]+", "", _market_context_fold(indicator.get("geography_level")).upper())
    label = _text(indicator.get("geography_label") or indicator.get("geography_name"))
    if level in {"NATIONAL", "COUNTRY", "CHILE"}:
        method_region = indicator.get("methodology_region") or indicator.get("coverage_region") or indicator.get("observed_region")
        if method_region and (not region or _market_context_region_key(method_region) != _market_context_region_key(region)):
            return False, "", 0
        return bool(label), label, 2
    if level in {"REGION", "REGIONAL"}:
        candidate = indicator.get("region") or indicator.get("geography_code") or label
        if region and _market_context_region_key(candidate) == _market_context_region_key(region):
            return True, label or _text(candidate), 3
    elif level == "MARKET":
        code = re.sub(r"[^A-Z0-9]+", "_", _text(indicator.get("geography_code")).upper()).strip("_")
        if code == "GRAN_SANTIAGO" and _market_context_region_key(region) == "metropolitana":
            return True, label or "Gran Santiago", 3
    elif level in {"COMMUNE", "COMUNA", "LOCAL"}:
        candidate = indicator.get("commune") or indicator.get("geography_name") or label
        if commune and _market_context_region_key(candidate) == _market_context_region_key(commune):
            return True, label or _text(candidate), 3
    return False, "", 0


def _market_context_numeric_value(item: Mapping[str, Any]) -> float | None:
    value = item.get("numeric_value", item.get("value"))
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return _number_from_label(value)


def _market_context_trend(current: Any, previous: Any) -> str:
    current_number = _number_from_label(current)
    previous_number = _number_from_label(previous)
    if current_number is None or previous_number is None:
        return "NO_PREVIOUS_DATA"
    difference = current_number - previous_number
    if abs(difference) <= 0.05:
        return "STABLE"
    return "RISING" if difference > 0 else "FALLING"


def _market_context_period_phrase(value: Any) -> str:
    period = _text(value)
    quarter = re.search(r"\b([1-4])\s*T\s*(\d{4})\b", period, flags=re.IGNORECASE)
    if quarter:
        ordinal = {"1": "primer", "2": "segundo", "3": "tercer", "4": "cuarto"}[quarter.group(1)]
        return f"el {ordinal} trimestre de {quarter.group(2)}"
    month_year = re.fullmatch(r"([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+(\d{4})(?:\s*·.*)?", period)
    if month_year:
        return f"{month_year.group(1).casefold()} de {month_year.group(2)}"
    return period


def _market_context_detail(item: Mapping[str, Any], family: str) -> str:
    """Narrate observed macro signals conditionally, without defining indicators."""
    kind = _text(item.get("kind"))
    current = _text(item.get("value"))
    previous = _text(item.get("previous_value"))
    trend = _text(item.get("trend_status")) or "NO_PREVIOUS_DATA"
    period = _market_context_period_phrase(item.get("period"))
    region = _text(item.get("geography_label")) or "la región"

    if kind == "MORTGAGE_REFERENCE" and family == "VENTA_RM":
        start = "La tasa hipotecaria de referencia en la Región Metropolitana"
        if trend == "FALLING":
            return f"{start} bajó de {previous} a {current}. La reducción mejora gradualmente las condiciones de financiamiento para compradores que dependen de crédito, aunque el precio de entrada y el dividendo continúan siendo factores relevantes al comparar alternativas."
        if trend == "RISING":
            return f"{start} subió de {previous} a {current}. El mayor costo del financiamiento puede aumentar la sensibilidad de los compradores al precio y al dividendo asociado a la propiedad."
        if trend == "STABLE":
            return f"{start} se mantiene prácticamente sin cambios en {current}. El costo del financiamiento no muestra una variación relevante respecto del período anterior, por lo que precio y condiciones de compra continúan teniendo un peso importante en la decisión."
        return f"{start} se ubica en {current}. El financiamiento continúa siendo una variable relevante en la decisión de compra, especialmente para hogares que dependen de crédito. Sin una comparación anterior verificada, no corresponde interpretar esta cifra como una mejora o deterioro de las condiciones de financiamiento."

    if kind == "UNEMPLOYMENT":
        national = item.get("national_comparison") if isinstance(item.get("national_comparison"), Mapping) else {}
        national_value = _text(national.get("value"))
        regional_number = _market_context_numeric_value(item)
        national_number = _market_context_numeric_value(national)
        if not national_value or regional_number is None or national_number is None:
            base = f"La desocupación en {region} alcanza {current}. No hay una comparación nacional verificada para el mismo período, por lo que no corresponde establecer una diferencia territorial."
        else:
            difference = regional_number - national_number
            if difference > 0.1:
                relative = "levemente por sobre" if difference <= 0.3 else "por sobre"
                base = f"La desocupación en {region} alcanza {current}, {relative} el {national_value} registrado a nivel nacional. Esto muestra un mercado laboral regional relativamente menos favorable que el promedio del país, un escenario que puede reforzar la cautela de los hogares frente a compromisos financieros relevantes."
                if trend == "RISING":
                    base += " Además, la tasa aumentó respecto del período anterior, reforzando esa señal de mayor cautela."
                elif trend == "FALLING":
                    base += " Sin embargo, la tasa disminuyó respecto del período anterior, lo que representa una mejora relativa dentro de un nivel que todavía se mantiene por sobre el promedio nacional."
                elif trend == "STABLE":
                    base += " La tasa se mantiene prácticamente sin cambios respecto del período anterior."
            elif difference < -0.1:
                relative = "levemente por debajo del" if difference >= -0.3 else "por debajo del"
                base = f"La desocupación en {region} alcanza {current}, {relative} {national_value} nacional. El mercado laboral regional presenta así una condición relativamente más favorable que el promedio del país, lo que entrega un contexto algo más favorable para la capacidad de los hogares de asumir compromisos."
                if trend == "RISING":
                    base += " En el período más reciente, la tasa aumentó, moderando esa señal favorable."
                elif trend == "FALLING":
                    base += " Además, la tasa disminuyó respecto del período anterior, reforzando la mejora observada."
                elif trend == "STABLE":
                    base += " La tasa se mantiene prácticamente sin cambios respecto del período anterior."
            else:
                base = f"La desocupación en {region} alcanza {current}, muy próxima al {national_value} nacional. El mercado laboral regional se encuentra en una situación similar a la observada en el conjunto del país, sin una diferencia territorial relevante en este indicador."
                if trend == "RISING":
                    base += " En el período más reciente, la tasa aumentó."
                elif trend == "FALLING":
                    base += " En el período más reciente, la tasa disminuyó."
                elif trend == "STABLE":
                    base += " La tasa también se mantiene prácticamente estable frente al período anterior."
        return base

    if kind == "REGIONAL_HOME_SALES" and family.startswith("VENTA_"):
        geography = _text(item.get("geography_label")) or region
        value = _market_context_numeric_value(item)
        observation = _market_context_period_phrase(item.get("period")) or "el período informado"
        if value is None:
            return ""
        if value < -0.05:
            amount = current.lstrip("+-− ")
            return f"Las ventas de viviendas en {geography} cayeron {amount} interanual durante {observation}. La señal es de menor dinamismo respecto del año anterior, por lo que captar demanda puede exigir un posicionamiento más competitivo frente a las alternativas disponibles."
        if value > 0.05:
            return f"Las ventas de viviendas en {geography} aumentaron {current} interanual durante {observation}. La actividad muestra mayor dinamismo que un año atrás, lo que entrega un entorno comercial más activo, aunque la respuesta de cada propiedad continúa dependiendo de su precio, ubicación y características."
        return f"Las ventas de viviendas en {geography} muestran poca variación respecto del año anterior durante {observation}. La actividad del mercado se mantiene relativamente estable, sin una señal clara de expansión o contracción."

    if kind == "TPM" and family.startswith("VENTA_"):
        if trend == "FALLING":
            return f"La TPM bajó de {previous} a {current}. La orientación monetaria se está haciendo menos restrictiva, una evolución que puede favorecer gradualmente las condiciones financieras, aunque su efecto sobre el crédito hipotecario no es inmediato."
        if trend == "RISING":
            return f"La TPM subió de {previous} a {current}. La orientación monetaria se vuelve más restrictiva, lo que puede limitar una mejora rápida de las condiciones generales de financiamiento."
        if trend == "STABLE":
            return f"La TPM permanece en {current}. No existe un cambio relevante de política monetaria respecto del período anterior, por lo que el escenario financiero general presenta continuidad en esta variable."
        return f"La TPM se sitúa en {current}" + (f" durante {period}" if period else "") + ". Sin una comparación anterior validada en la serie del portal, debe utilizarse como parte del escenario financiero general y no como una señal aislada de mayor o menor demanda inmobiliaria."

    if kind == "REAL_WAGES" and family.startswith("ARRIENDO_"):
        value = _market_context_numeric_value(item)
        observation = period or "el período informado"
        if value is None:
            return ""
        if value > 0.05:
            return f"Las remuneraciones reales aumentaron {current} interanual en {observation}. Esto indica que, en promedio, los ingresos laborales crecieron por sobre la variación de precios, entregando un contexto relativamente más favorable para la capacidad de pago de los hogares."
        if value < -0.05:
            return f"Las remuneraciones reales disminuyeron {current.lstrip('-− ')} interanual en {observation}. La pérdida de poder adquisitivo puede aumentar la sensibilidad de los hogares frente al valor mensual del arriendo y otros gastos asociados a la vivienda."
        return f"Las remuneraciones reales presentan escasa variación interanual en {observation}. El poder adquisitivo laboral no muestra un avance significativo, por lo que la capacidad de pago de los hogares permanece relativamente estable."

    if kind == "CPI" and family.startswith("ARRIENDO_"):
        if trend == "RISING":
            return f"El IPC registra una variación anual de {current}, superior a la observada en el período anterior. El costo general de vida está aumentando a un ritmo mayor, lo que puede ejercer más presión sobre el presupuesto disponible de los hogares para vivienda."
        if trend == "FALLING":
            return f"El IPC registra una variación anual de {current}, inferior a la observada en el período anterior. La presión del costo de vida se está moderando, aunque los precios continúan siendo superiores a los de un año atrás."
        if trend == "STABLE":
            return f"El IPC registra una variación anual de {current}, sin un cambio relevante respecto del período anterior. El costo de vida continúa siendo un factor a considerar en el presupuesto mensual de los hogares."
        return f"El IPC registra una variación anual de {current}. Esto indica que el costo general de vida es superior al de un año atrás y aporta contexto sobre la presión que enfrenta el presupuesto de los hogares. No debe interpretarse como una medición directa del precio de los arriendos."

    # A regional rental series needs explicit semantic metadata before its
    # direction can safely be narrated. Until then, show its verified value only.
    if kind == "REGIONAL_RENTAL_INDICATOR":
        return ""
    return f"Dato observado: {current}." if current else ""


_MARKET_CONTEXT_NARRATIVE_FAMILIES = {
    "VENTA_RM": {
        "reading": "La lectura reúne señales financieras, laborales y de actividad del mercado de compra.",
        "implication": "Al revisar una operación de venta, estas referencias pueden acompañar la evaluación del precio y del seguimiento comercial.",
    },
    "ARRIENDO_RM": {
        "reading": "La lectura reúne señales laborales, de ingresos y del entorno de arriendo en la Región Metropolitana.",
        "implication": "Al revisar una operación de arriendo, estas referencias pueden acompañar la evaluación de las condiciones y del seguimiento comercial.",
    },
    "VENTA_REGION": {
        "reading": "La lectura reúne señales laborales, financieras y de actividad de compra disponibles para {region}.",
        "implication": "Al revisar una operación de venta, estas referencias pueden acompañar la evaluación del precio y del seguimiento comercial.",
    },
    "ARRIENDO_REGION": {
        "reading": "La lectura reúne señales laborales, de ingresos y del entorno de arriendo disponibles para {region}.",
        "implication": "Al revisar una operación de arriendo, estas referencias pueden acompañar la evaluación de las condiciones y del seguimiento comercial.",
    },
}


def _market_context_family(operation: str, region: str) -> str:
    is_rm = _market_context_region_key(region) == "metropolitana"
    return f"{operation}_{'RM' if is_rm else 'REGION'}"


def _market_context_narrative(context: Mapping[str, Any] | None) -> str:
    """Build one of four generic, snapshot-driven narratives; never use property data."""
    if not isinstance(context, Mapping):
        return ""
    operation = _market_context_operation(context.get("operation"))
    if not operation:
        return ""
    region = _text(context.get("geography", {}).get("region")) if isinstance(context.get("geography"), Mapping) else _text(context.get("region"))
    family = _market_context_family(operation, region)
    template = _MARKET_CONTEXT_NARRATIVE_FAMILIES.get(family)
    if not template:
        return ""
    indicators = context.get("indicators") if isinstance(context.get("indicators"), list) else []
    primary = context.get("visible_kpis") if isinstance(context.get("visible_kpis"), list) else indicators
    signals = []
    for item in primary:
        if not isinstance(item, Mapping) or not _text(item.get("value")):
            continue
        kind = _text(item.get("kind"))
        value = _text(item.get("value"))
        previous = _text(item.get("previous_value"))
        trend = _text(item.get("trend_status")) or "NO_PREVIOUS_DATA"
        if kind == "MORTGAGE_REFERENCE":
            signal = {
                "FALLING": f"la tasa hipotecaria bajó de {previous} a {value}",
                "RISING": f"la tasa hipotecaria subió de {previous} a {value}",
                "STABLE": f"la tasa hipotecaria se mantuvo en {value}",
                "NO_PREVIOUS_DATA": f"la tasa hipotecaria se ubicó en {value}, sin comparación anterior verificada",
            }[trend]
        elif kind == "UNEMPLOYMENT":
            national = item.get("national_comparison") if isinstance(item.get("national_comparison"), Mapping) else {}
            national_value = _text(national.get("value"))
            difference = None
            if national_value:
                region_number = _market_context_numeric_value(item)
                national_number = _market_context_numeric_value(national)
                if region_number is not None and national_number is not None:
                    difference = region_number - national_number
            if difference is None:
                signal = f"la desocupación regional se ubicó en {value}, sin comparación nacional verificada"
            elif difference > 0.1:
                signal = f"la desocupación regional fue {value}, {abs(difference):.1f} puntos porcentuales por sobre el {national_value} nacional".replace(".", ",")
            elif difference < -0.1:
                signal = f"la desocupación regional fue {value}, {abs(difference):.1f} puntos porcentuales por debajo del {national_value} nacional".replace(".", ",")
            else:
                signal = f"la desocupación regional fue {value}, muy próxima al {national_value} nacional"
            if trend in {"RISING", "FALLING", "STABLE"}:
                signal += {"RISING": " y aumentó frente al período anterior", "FALLING": " y disminuyó frente al período anterior", "STABLE": " y se mantuvo respecto del período anterior"}[trend]
        elif kind == "REGIONAL_HOME_SALES":
            number = _market_context_numeric_value(item)
            signal = (
                f"las ventas de viviendas en {_text(item.get('geography_label'))} cayeron {value} interanual"
                if number is not None and number < -0.05 else
                f"las ventas de viviendas en {_text(item.get('geography_label'))} aumentaron {value} interanual"
                if number is not None and number > 0.05 else
                f"las ventas de viviendas en {_text(item.get('geography_label'))} variaron poco interanualmente"
            )
        elif kind == "TPM":
            signal = {
                "FALLING": f"la TPM bajó de {previous} a {value}",
                "RISING": f"la TPM subió de {previous} a {value}",
                "STABLE": f"la TPM se mantuvo en {value}",
                "NO_PREVIOUS_DATA": f"la TPM se ubicó en {value}, sin comparación anterior validada",
            }[trend]
        elif kind == "REAL_WAGES":
            number = _market_context_numeric_value(item)
            signal = (
                f"las remuneraciones reales aumentaron {value}"
                if number is not None and number > 0.05 else
                f"las remuneraciones reales disminuyeron {value.lstrip('-− ')}"
                if number is not None and number < -0.05 else
                f"las remuneraciones reales variaron poco ({value})"
            )
        elif kind == "CPI":
            signal = {
                "RISING": f"la variación anual del IPC se aceleró hasta {value}",
                "FALLING": f"la variación anual del IPC se moderó a {value}",
                "STABLE": f"la variación anual del IPC se mantuvo en {value}",
                "NO_PREVIOUS_DATA": f"la variación anual del IPC se ubicó en {value}, sin comparación anterior verificada",
            }[trend]
        else:
            continue
        signals.append(signal)
    if not signals:
        return ""
    reference_month = _text(context.get("reference_month"))
    reading = template["reading"].format(region=region or "la región")
    period_lead = f"Durante {reference_month}, " if reference_month else "En el período observado, "
    return f"{period_lead}{'; '.join(signals)}. {reading} {template['implication']}"


def _market_indicator_display_value(raw: Mapping[str, Any], kind: str) -> str:
    explicit = _text(raw.get("display_value"))
    if explicit:
        return explicit
    value = raw.get("value")
    change_kinds = {"REAL_WAGES", "CPI", "REGIONAL_HOME_SALES"}
    if kind in change_kinds and raw.get("change_display"):
        return _text(raw.get("change_display"))
    if kind in change_kinds and raw.get("change") is not None:
        value = raw.get("change")
    if value is None:
        return ""
    percentage_kinds = {"MORTGAGE_REFERENCE", "UNEMPLOYMENT", "TPM"}
    default_percent = kind in percentage_kinds or (kind in change_kinds and raw.get("change") is not None)
    unit = _text(raw.get("unit")) or ("%" if default_percent else "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rendered = f"{value:,.1f}".replace(",", "\0").replace(".", ",").replace("\0", ".")
        if value > 0 and raw.get("change") == value:
            rendered = "+" + rendered
        return f"{rendered}%" if unit == "%" else f"{rendered} {unit}" if unit else ""
    value_text = _text(value)
    if not unit or value_text.endswith(unit) or (unit == "%" and value_text.endswith("%")):
        return value_text
    return f"{value_text}{unit}" if unit == "%" else f"{value_text} {unit}"


def _market_context_identity(raw: Mapping[str, Any]) -> tuple[str, str, str]:
    kind = _market_context_kind(raw.get("kind") or raw.get("indicator_kind"))
    level = re.sub(r"[^A-Z]+", "", _text(raw.get("geography_level")).upper())
    code = re.sub(r"[^A-Z0-9]+", "_", _text(raw.get("geography_code")).upper()).strip("_")
    if not code:
        code = _market_context_fold(raw.get("geography_label") or raw.get("geography_name"))
    return kind, level, code


def _normalize_market_context(
    context: Mapping[str, Any] | None, *, operation: Any = "", region: Any = "", commune: Any = "",
    property_price_uf: Any = None, property_condition: Any = "",
) -> dict[str, Any] | None:
    """Resolve a compact macro context from verified, operation/geography-aware snapshot indicators."""
    if not isinstance(context, Mapping):
        return None
    operation_key = _market_context_operation(operation)
    if not operation_key:
        return None
    region_label, commune_label = _text(region), _text(commune)
    raw_indicators = context.get("indicators")
    if not isinstance(raw_indicators, list):
        # Legacy campaign cards do not include enough verification or geography
        # metadata to safely reuse their values in a property-specific context.
        return None
    is_rm = _market_context_region_key(region_label) == "metropolitana"
    if operation_key == "VENTA":
        # Explicit, operation-aware ranks. Geography and verified provenance
        # are hard filters before relevance can affect display order.
        allowed = (
            ("MORTGAGE_REFERENCE", "UNEMPLOYMENT", "REGIONAL_HOME_SALES", "TPM")
            if is_rm else ("UNEMPLOYMENT", "TPM", "REGIONAL_HOME_SALES")
        )
        priority = {kind: score for kind, score in zip(allowed, (100, 95, 90, 70))}
    else:
        allowed = ("UNEMPLOYMENT", "REAL_WAGES", "REGIONAL_RENTAL_INDICATOR", "CPI")
        priority = {kind: score for kind, score in zip(allowed, (100, 95, 90, 75))}
    candidates: dict[str, list[tuple[int, Mapping[str, Any], dict[str, Any]]]] = {}
    for raw in raw_indicators:
        if not isinstance(raw, Mapping):
            continue
        is_available = raw.get("available") is True or (raw.get("active") is True and raw.get("verified") is True)
        if not is_available or raw.get("verified") is not True:
            continue
        kind = _market_context_kind(raw.get("kind") or raw.get("indicator_kind"))
        if kind not in allowed:
            continue
        period = _text(raw.get("period_label") or raw.get("observation_period") or raw.get("period"))
        if kind == "REGIONAL_RENTAL_INDICATOR":
            # A rental series can be a primary KPI only when its actual
            # observation period and explicit regional scope are present.
            level = re.sub(r"[^A-Z]+", "", _text(raw.get("geography_level")).upper())
            if not period or level not in {"REGION", "REGIONAL"} or not _text(raw.get("geography_label") or raw.get("geography_name")):
                continue
        relevant_for = raw.get("relevant_for") or raw.get("relevant_operations")
        if isinstance(relevant_for, str):
            relevant_for = [relevant_for]
        if isinstance(relevant_for, (list, tuple, set)) and relevant_for:
            relevance = {_market_context_operation(item) for item in relevant_for}
            if operation_key not in relevance and "" not in relevance:
                continue
        source = _text(raw.get("source") or raw.get("source_name"))
        value = _market_indicator_display_value(raw, kind)
        if not source or not value or value.casefold() in {"n/a", "no disponible", "—", "-"}:
            continue
        geo_ok, geo_label, geo_rank = _market_context_geo(raw, region=region_label, commune=commune_label)
        if not geo_ok:
            continue
        level = re.sub(r"[^A-Z]+", "", _text(raw.get("geography_level")).upper())
        if kind == "UNEMPLOYMENT" and level not in {"REGION", "REGIONAL", "NATIONAL", "COUNTRY"}:
            continue
        if kind == "REGIONAL_HOME_SALES":
            code = re.sub(r"[^A-Z0-9]+", "_", _text(raw.get("geography_code")).upper()).strip("_")
            if is_rm:
                if level != "MARKET" or code != "GRAN_SANTIAGO":
                    continue
            elif level not in {"REGION", "REGIONAL"} or geo_rank < 3:
                continue
        note = " · ".join(part for part in (geo_label, period, _text(raw.get("note"))) if part)
        if kind == "UNEMPLOYMENT" and raw.get("unemployment_yoy_change_pp") is not None:
            change = _market_indicator_display_value({
                "value": raw.get("unemployment_yoy_change_pp"), "unit": "pp", "change": raw.get("unemployment_yoy_change_pp"),
            }, kind)
            note = " · ".join(part for part in (note, f"Variación anual {change}") if part)
        item = {
            "kind": kind, "label": _MARKET_CONTEXT_LABELS[kind], "value": value, "note": note,
            "geography_level": _text(raw.get("geography_level")).upper(), "geography_label": geo_label,
            "period": period, "source": source,
            "source_date": raw.get("source_date") or raw.get("source_published_at") or raw.get("cutoff"),
            "change": raw.get("change"),
            "numeric_value": raw.get("change") if kind in {"REAL_WAGES", "CPI", "REGIONAL_HOME_SALES"} and raw.get("change") is not None else raw.get("value"),
            "_identity": _market_context_identity(raw),
            "_observation_period": _text(raw.get("observation_period") or raw.get("period_label") or raw.get("period")),
        }
        candidates.setdefault(kind, []).append((geo_rank, raw, item))

    selected: list[dict[str, Any]] = []
    for kind in allowed:
        options = candidates.get(kind, [])
        if not options:
            continue
        if kind == "UNEMPLOYMENT":
            options.sort(key=lambda item: item[0], reverse=True)
            chosen = next((item for item in options if item[0] == 3), None)
            national = next((item for item in options if item[0] == 2), None)
            if chosen and national:
                regional_observation = _text(chosen[1].get("observation_period") or chosen[1].get("period_label") or chosen[1].get("period"))
                national_observation = _text(national[1].get("observation_period") or national[1].get("period_label") or national[1].get("period"))
                if regional_observation == national_observation:
                    chosen[2]["national_comparison"] = {
                        "value": national[2]["value"],
                        "numeric_value": national[2]["numeric_value"],
                        "source": national[2]["source"],
                        "period": national_observation,
                        "geography_code": _text(national[1].get("geography_code")),
                    }
                    regional_number = _market_context_numeric_value(chosen[2])
                    national_number = _market_context_numeric_value(chosen[2]["national_comparison"])
                    if regional_number is not None and national_number is not None:
                        difference = regional_number - national_number
                        chosen[2]["national_comparison_difference_pp"] = round(difference, 2)
                        chosen[2]["national_comparison_status"] = (
                            "ABOVE_NATIONAL" if difference > 0.1 else
                            "BELOW_NATIONAL" if difference < -0.1 else "NEAR_NATIONAL"
                        )
        else:
            options.sort(key=lambda item: (item[0], _text(item[1].get("source_date") or item[1].get("cutoff") or item[1].get("period"))), reverse=True)
            chosen = options[0]
        if chosen:
            selected.append(chosen[2])

    if operation_key == "ARRIENDO" and any(item["kind"] == "REGIONAL_RENTAL_INDICATOR" for item in selected):
        selected = [item for item in selected if item["kind"] != "CPI"]

    previous_indicators = context.get("previous_indicators") if isinstance(context.get("previous_indicators"), list) else []
    previous_by_identity: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for previous in previous_indicators:
        if not isinstance(previous, Mapping) or previous.get("verified") is not True or previous.get("active") is not True:
            continue
        previous_by_identity.setdefault(_market_context_identity(previous), []).append(previous)
    previous_period = _text(context.get("previous_period"))
    for item in selected:
        identity = item.pop("_identity")
        current_observation = item.pop("_observation_period")
        matches = previous_by_identity.get(identity, [])
        previous = matches[0] if len(matches) == 1 else None
        if previous and previous_period:
            expected_previous_period = _text(previous.get("period"))
            if expected_previous_period != previous_period:
                previous = None
        if previous:
            previous_display = _market_indicator_display_value(previous, item["kind"])
            previous_numeric = previous.get("change") if item["kind"] in {"REAL_WAGES", "CPI", "REGIONAL_HOME_SALES"} and previous.get("change") is not None else previous.get("value")
            item.update({
                "previous_value": previous_display,
                "previous_period": previous_period,
                "trend_status": _market_context_trend(item.get("numeric_value"), previous_numeric),
            })
        else:
            item.update({"previous_value": "", "previous_period": None, "trend_status": "NO_PREVIOUS_DATA"})
        item["observation_period"] = current_observation
        item["detail"] = _market_context_detail(item, _market_context_family(operation_key, region_label))

    selected.sort(key=lambda item: priority.get(item["kind"], 0), reverse=True)

    selected = selected[:4]
    visible_kpis = [
        item for item in selected
        if not (operation_key == "VENTA" and is_rm and item["kind"] == "TPM")
    ][:3]
    if not selected:
        return None

    month = _text(context.get("reference_month") or context.get("reference_period"))
    period = _text(context.get("period"))
    if not month and re.fullmatch(r"\d{4}-\d{2}", period):
        month_names = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
        year, month_number = period.split("-")
        index = int(month_number) - 1
        if 0 <= index < 12:
            month = f"{month_names[index]} {year}"
    sources = list(dict.fromkeys(
        source for item in selected
        for source in (item.get("source"), (item.get("national_comparison") or {}).get("source"))
        if source
    ))
    details = ([{
        "title": item["label"], "body": item["detail"],
        "context": " · ".join(part for part in (item.get("geography_label"), item.get("period"), item.get("source")) if part),
        "period": item.get("period"), "source": item["source"],
    } for item in selected if item.get("detail")])[:5]
    dates = [_source_date_label(item.get("source_date")) for item in selected if item.get("source_date")]
    result = {
        "operation": operation_key, "reference_month": month,
        "geography": {"country": "Chile", "region": region_label, "commune": commune_label},
        "indicators": selected, "kpis": visible_kpis, "visible_kpis": visible_kpis,
        "narrative_family": _market_context_family(operation_key, region_label),
        "detail_blocks": details,
        "source_date": _text(context.get("source_date") or (dates[0] if dates else "")) or None,
        "sources": " · ".join(sources) if sources else None,
    }
    result["summary"] = _market_context_narrative(result)
    return result


def _report_market_context_period(monthly: Mapping[str, Any], snapshot: Mapping[str, Any], campaign_view: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """Return the report's frozen period; never substitute today's/latest period."""
    frozen_campaign_snapshot = campaign_view.get("snapshot") if isinstance(campaign_view.get("snapshot"), Mapping) else {}
    for value in (
        monthly.get("period"), monthly.get("generated_at"), snapshot.get("prepared_at"),
        frozen_campaign_snapshot.get("prepared_at"),
        campaign_view.get("sent_at"), row.get("sent_at"),
    ):
        period = _as_period(value)
        if period:
            return period
    return ""


def _load_market_context_snapshot(db: Any, period: str) -> Mapping[str, Any] | None:
    """Load the selected snapshot plus only its immediately previous month."""
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(period or "")):
        return None

    target_year, target_month = (int(part) for part in period.split("-"))
    target_ordinal = target_year * 12 + target_month - 1
    try:
        collection = db[MARKET_CONTEXT_SNAPSHOT_COLLECTION]
        exact = [
            item for item in collection.find({"period": period, "active": True}, {"_id": 0})
            if isinstance(item, Mapping)
        ]
        eligible = exact or [
            item for item in collection.find({"period": {"$lte": period}, "active": True}, {"_id": 0})
            if isinstance(item, Mapping)
            and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(item.get("period") or ""))
            and str(item.get("period")) <= period
        ]
    except Exception:
        logging.getLogger(__name__).exception("Unable to read owner market context snapshot")
        return None

    if not eligible:
        return None
    selected_period = max(str(item["period"]) for item in eligible)
    selected_year, selected_month = (int(part) for part in selected_period.split("-"))
    selected_ordinal = selected_year * 12 + selected_month - 1
    age_months = target_ordinal - selected_ordinal
    if age_months < 0 or age_months > 1:
        return None
    selected = [item for item in eligible if str(item["period"]) == selected_period]
    selected_year, selected_month = (int(part) for part in selected_period.split("-"))
    previous_ordinal = selected_year * 12 + selected_month - 2
    previous_year, previous_month0 = divmod(previous_ordinal, 12)
    previous_period = f"{previous_year:04d}-{previous_month0 + 1:02d}"
    try:
        previous = [
            item for item in db[MARKET_CONTEXT_SNAPSHOT_COLLECTION].find(
                {"period": previous_period, "active": True}, {"_id": 0},
            ) if isinstance(item, Mapping)
        ]
    except Exception:
        logging.getLogger(__name__).exception("Unable to read previous owner market context snapshot")
        previous = []
    return {
        "period": selected_period,
        "indicators": selected,
        "previous_period": previous_period if previous else None,
        "previous_indicators": previous,
    }


def _previous_snapshot_changes(snapshots: Any, current: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return only explicit metric pairs present in consecutive monthly snapshots."""
    if not isinstance(snapshots, list):
        return []
    period = _as_period(current.get("period") or current.get("generated_at"))
    previous = [item for item in snapshots if isinstance(item, Mapping)
                and _as_period(item.get("period")) and _as_period(item.get("period")) < period]
    if not previous:
        return []
    prior = max(previous, key=lambda item: _as_period(item.get("period")))

    def metric(snapshot: Mapping[str, Any], key: str, nested: str | None = None) -> Any:
        if key in snapshot:
            return snapshot.get(key)
        child = snapshot.get(nested) if nested else None
        return child.get(key) if isinstance(child, Mapping) else None

    changes: list[dict[str, str]] = []
    definitions = (
        ("Leads 90 días", "leads", "activity_90d"),
        ("Visitas coordinadas", "visits", "activity_90d"),
    )
    for label, key, nested in definitions:
        old, new = metric(prior, key, nested), metric(current, key, nested)
        if old is None or new is None:
            continue
        changes.append({"label": label, "before": str(old), "after": str(new)})
    old_price, new_price = metric(prior, "current_price"), metric(current, "current_price")
    old_operation, new_operation = _text(prior.get("operation")), _text(current.get("operation"))
    if old_price is not None and new_price is not None and old_operation and old_operation == new_operation:
        from .campaign import _format_client_price
        changes.append({
            "label": "Precio",
            "before": _format_client_price(old_price, old_operation),
            "after": _format_client_price(new_price, new_operation),
        })
    old_comps = prior.get("comparables") or {}
    new_comps = current.get("comparables") or {}
    if isinstance(old_comps, Mapping) and isinstance(new_comps, Mapping):
        old_gap = _position_gap(old_comps.get("reference_value"), old_comps.get("property_value"), _text(old_comps.get("unit")))
        new_gap = _position_gap(new_comps.get("reference_value"), new_comps.get("property_value"), _text(new_comps.get("unit")))
        if old_gap is not None and new_gap is not None:
            changes.append({"label": "Posición vs mercado", "before": f"{old_gap:+.0f}%", "after": f"{new_gap:+.0f}%"})
    return changes


def _gap_explanation(position: Mapping[str, Any], adjustment: Any) -> str:
    if _position_gap(position.get("reference_value"), position.get("property_value"), _text(position.get("unit"))) is None:
        return ""
    reference = _number_from_label(position.get("reference_value"))
    subject = _number_from_label(position.get("property_value"))
    if reference is None or subject is None or reference <= 0 or subject <= reference or adjustment is None:
        return ""
    try:
        adjustment_pct = abs(float(str(adjustment).replace("%", "").replace(",", ".")))
    except (TypeError, ValueError):
        return ""
    gap_pct = ((subject / reference) - 1) * 100
    if gap_pct - adjustment_pct < 5:
        return ""
    return (
        f"La propiedad está aproximadamente {gap_pct:.0f}% sobre la referencia disponible. "
        f"El ajuste propuesto ({adjustment_pct:.0f}%) es menor que esa brecha: plantea una corrección acotada; "
        "la respuesta de la demanda permitirá evaluar el nuevo posicionamiento."
    )


def _recommendation_period_label(value: Any) -> str:
    period = _as_period(value)
    match = re.fullmatch(r"(\d{4})-(\d{2})", period)
    if not match:
        return ""
    months = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
    year, month = int(match.group(1)), int(match.group(2))
    return f"{months[month - 1].capitalize()} {year}" if 1 <= month <= 12 else ""


def _price_presentation(value: Any, exact_label: Any, operation: str, current_value: Any) -> tuple[str, str, Any]:
    """Create presentation-only rounded UF labels; leave stored/exact prices untouched."""
    from .campaign import _format_client_price

    exact = _text(exact_label)
    if not exact.casefold().endswith("uf"):
        return exact, "", value
    amount = _number_from_label(value)
    if amount is None or amount <= 0:
        return exact, "", value
    increment = Decimal("50") if amount >= 1000 else Decimal("10")
    rounded = (Decimal(str(amount)) / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment
    proposed_label = "≈ " + _format_client_price(float(rounded), operation)
    current_label = _text(_format_client_price(current_value, operation)) if current_value is not None else ""
    current_amount = _number_from_label(current_value)
    difference_label = ""
    if current_label.casefold().endswith("uf") and current_amount is not None and current_amount > float(rounded):
        difference = current_amount - float(rounded)
        difference_label = "≈ " + _format_client_price(difference, operation)
    return proposed_label, difference_label, int(rounded)


def _activity_count_signals(activity: Mapping[str, Any]) -> list[tuple[str, str]]:
    signals: list[tuple[str, str]] = []
    labels = (("leads", "lead", "leads"), ("conversations", "conversación", "conversaciones"), ("visits", "visita", "visitas"))
    for key, singular, plural in labels:
        value = activity.get(key)
        if isinstance(value, bool) or value is None:
            continue
        number = _number_from_label(value)
        if number is None or number < 0 or not re.fullmatch(
            r"\s*\d+(?:[.,]\d+)?\s*(?:(?:leads?(?: registrados?)?|conversaciones?(?: registradas?)?|visitas?(?: coordinadas?)?))?\s*",
            _text(value), flags=re.IGNORECASE,
        ):
            continue
        shown = str(int(number)) if number.is_integer() else _text(value).strip()
        signals.append((singular if number == 1 else plural, shown))
    return signals


def _recommendation_narrative(position: Mapping[str, Any], activity: Mapping[str, Any], adjustment: Any) -> tuple[str, list[str]]:
    """Build concise copy only from comparable and 90-day signals present in the snapshot."""
    gap_pct = position.get("gap_pct")
    has_comparable_values = bool(position.get("reference_value") and position.get("property_value") and gap_pct is not None)
    activity_signals = _activity_count_signals(activity)
    activity_copy = ""
    activity_detail = ""
    if activity_signals:
        counts = ", ".join(f"{value} {label}" for label, value in activity_signals)
        activity_copy = "En 90 días: " + counts + "."
        activity_detail = "Actividad registrada en los últimos 90 días: " + counts + "."

    if has_comparable_values and gap_pct > 0:
        summary = "El valor por m² de tu propiedad supera la referencia comparable."
        if activity_copy:
            summary = summary[:-1] + "; " + activity_copy[0].lower() + activity_copy[1:]
        else:
            summary = summary[:-1] + "; el ajuste busca reposicionar la publicación y observar su respuesta."
    elif activity_copy:
        summary = activity_copy[:-1] + "; el ajuste propone revisar el posicionamiento."
    else:
        summary = "El ajuste propuesto es una alternativa de posicionamiento para revisar antes de decidir."

    details: list[str] = []
    if has_comparable_values:
        details.append(
            f"Tu propiedad se publica en {position['property_value']} frente a {position['reference_value']} de referencia comparable."
        )
        try:
            adjustment_pct = abs(float(str(adjustment).replace("%", "").replace(",", ".")))
        except (TypeError, ValueError):
            adjustment_pct = None
        if gap_pct > 0 and adjustment_pct is not None and gap_pct - adjustment_pct >= 5:
            details.append(
                f"La brecha frente a la referencia comparable es aproximadamente {gap_pct:.0f}%; "
                f"el ajuste de {adjustment_pct:g}% es menor y no busca cerrar toda esa diferencia de una vez."
            )
    if activity_copy:
        details.append(activity_detail)
    if not details:
        details.append("El ajuste es una propuesta de revisión; no modifica el precio publicado por sí solo.")
    return summary, details[:3]


def build_monthly_portal_view(
    db: Any,
    row: Mapping[str, Any],
    campaign_view: Mapping[str, Any],
    *,
    email_html: str | None = None,
) -> dict[str, Any]:
    """Build the monthly report from an optional verified monthly snapshot.

    With no monthly record, render only frozen campaign fields and evidence
    extracted from the exact sent HTML. This function is read-only.
    """
    property_code = str(row.get("property_code") or "").strip()
    snapshot = row.get("campaign_snapshot") if isinstance(row.get("campaign_snapshot"), Mapping) else {}
    owner_email = str(snapshot.get("owner_email") or row.get("owner_email") or "").strip().casefold()
    record = None
    if property_code and owner_email:
        record = db[OWNER_PROPERTY_PORTAL_COLLECTION].find_one({
            "_id": owner_property_portal_id(property_code, owner_email),
            "property_code": property_code,
            "owner_key": owner_property_portal_id(property_code, owner_email),
        })
    monthly = _select_monthly_snapshot(record, property_code) or {}
    monthly_snapshots = record.get("monthly_snapshots", []) if isinstance(record, Mapping) else []
    property_media = verified_media_for_property(property_code, monthly=monthly, row=row)
    if property_media is None:
        property_media = verified_historical_media(email_html, property_code)
    evidence = extract_verified_email_evidence(email_html)
    property_state = monthly.get("property") if isinstance(monthly.get("property"), Mapping) else monthly
    raw_context = None
    context = None
    # Resolve activity once. The same object feeds both the summary KPI and
    # funnel, with a deterministic report cutoff for reconstruction.
    campaign_snapshot = campaign_view.get("snapshot") if isinstance(campaign_view.get("snapshot"), Mapping) else snapshot
    market_context_period = _report_market_context_period(monthly, snapshot, campaign_view, row)
    raw_context = _load_market_context_snapshot(db, market_context_period)
    # Compatibility for an explicitly frozen, verified monthly snapshot. Never
    # resurrect the old email macro block, which lacks canonical provenance.
    embedded_context = monthly.get("market_context")
    if (raw_context is None and isinstance(embedded_context, Mapping)
            and _as_period(embedded_context.get("period")) == market_context_period
            and isinstance(embedded_context.get("indicators"), list)):
        # Embedded campaign/monthly material is not the canonical rental
        # snapshot. Keep its other frozen indicators for compatibility, but
        # never promote a QA-only rental series into production presentation.
        raw_context = {
            **embedded_context,
            "indicators": [
                item for item in embedded_context["indicators"]
                if not isinstance(item, Mapping)
                or _market_context_kind(item.get("kind") or item.get("indicator_kind")) != "REGIONAL_RENTAL_INDICATOR"
            ],
        }
    saved_email_model = next((
        source.get(key) for source in (snapshot, campaign_snapshot, row)
        for key in ("email_render_model", "render_model")
        if isinstance(source.get(key), Mapping)
    ), {})
    campaign_activity = campaign_view.get("activity_90d")
    if not isinstance(campaign_activity, Mapping):
        campaign_activity = campaign_snapshot.get("activity_90d") if isinstance(campaign_snapshot.get("activity_90d"), Mapping) else {}
    saved_activity = saved_email_model.get("activity_90d") if isinstance(saved_email_model, Mapping) else {}
    frozen_activities = (
        campaign_activity, saved_activity, campaign_snapshot.get("activity_90d"),
        snapshot.get("activity_90d"), evidence.get("activity_90d"),
    )
    report_cutoffs = (
        (monthly.get("activity_90d") or {}).get("window_end") if isinstance(monthly.get("activity_90d"), Mapping) else None,
        (monthly.get("activity_90d") or {}).get("cutoff_at") if isinstance(monthly.get("activity_90d"), Mapping) else None,
        snapshot.get("prepared_at"), campaign_snapshot.get("prepared_at"),
        campaign_view.get("sent_at"), row.get("sent_at"),
    )
    operation_for_activity = _text(
        _value(property_state, snapshot, "operation") or snapshot.get("operation_resolved")
        or row.get("operation") or campaign_view.get("operation")
    ).upper()
    commercial_funnel = resolve_owner_commercial_funnel_90d(
        db, property_code,
        candidates=(monthly.get("activity_90d"), *frozen_activities),
        window_end_candidates=report_cutoffs,
        operation=operation_for_activity,
    )
    # Wave 2 froze this lead total at campaign preparation time, but stored it
    # at the top level of campaign_snapshot rather than under activity_90d.
    # For price-review pages, retain that verified historical lead count while
    # leaving visits/offers/closings to their own verified or unavailable
    # sources. The snapshot cutoff keeps this count tied to the campaign window.
    frozen_campaign_leads = _activity_count(campaign_snapshot.get("leads_90d"))
    frozen_campaign_cutoff = _activity_datetime(campaign_snapshot.get("prepared_at"))
    campaign_price_review_required = (
        str(row.get("send_status") or "").upper() == "SKIPPED_STALE_OR_MISMATCH"
        or bool(campaign_view.get("price_review_required"))
    )
    if campaign_price_review_required and frozen_campaign_leads is not None and frozen_campaign_cutoff is not None:
        funnel_rows = {
            str(item.get("key")): {
                "count": item.get("count"),
                "status": item.get("status"),
                "source": item.get("source"),
            }
            for item in commercial_funnel.get("stages", [])
            if isinstance(item, Mapping) and item.get("key")
        }
        funnel_rows["LEADS"] = {
            "count": frozen_campaign_leads,
            "status": "VERIFIED",
            "source": "campaign_snapshot.leads_90d",
        }
        frozen_funnel = _build_owner_funnel(
            funnel_rows,
            source="CAMPAIGN_SNAPSHOT_LEADS_WITH_CANONICAL_ACTIVITY",
            cutoff=frozen_campaign_cutoff,
            start=frozen_campaign_cutoff - timedelta(days=90),
        )
        for key in ("conversations", "summary", "integrity"):
            if key in commercial_funnel:
                frozen_funnel[key] = commercial_funnel[key]
        commercial_funnel = frozen_funnel
    funnel_counts = {
        item.get("key"): item.get("count")
        for item in commercial_funnel.get("stages", [])
        if isinstance(item, Mapping)
    }
    activity = {
        "leads": funnel_counts.get("LEADS"),
        "visits": funnel_counts.get("VISITS"),
        "conversations": commercial_funnel.get("conversations"),
        "source": commercial_funnel.get("source"),
        "summary": commercial_funnel.get("summary", ""),
        "state": commercial_funnel.get("source_status"),
        "window_start": commercial_funnel.get("start_date"),
        "window_end": commercial_funnel.get("cutoff"),
        "integrity": commercial_funnel.get("source_status"),
    }
    communal = monthly.get("communal_reference") if isinstance(monthly.get("communal_reference"), Mapping) else {}
    if not communal:
        compact = (evidence.get("market-reference-compact-single") or [None])[0]
        communal = {"summary": compact} if compact else {}
    if communal.get("source_date"):
        communal = {**communal, "source_date": _source_date_label(communal.get("source_date"))}
    recommendation = monthly.get("recommendation") if isinstance(monthly.get("recommendation"), Mapping) else {}
    comparable_state = monthly.get("comparables") if isinstance(monthly.get("comparables"), Mapping) else {}
    position = _position_data(evidence, monthly)

    prepared_at = (
        monthly.get("generated_at") or monthly.get("prepared_at")
        or snapshot.get("prepared_at") or campaign_view.get("sent_at")
    )
    if isinstance(prepared_at, datetime):
        updated_label = prepared_at.astimezone(timezone.utc).strftime("%d-%m-%Y")
    elif isinstance(prepared_at, date):
        updated_label = prepared_at.strftime("%d-%m-%Y")
    else:
        updated_label = _text(prepared_at)[:10]

    status = str(row.get("send_status") or "").upper()
    stale_status = status == "SKIPPED_STALE_OR_MISMATCH"
    price_review_required = stale_status or bool(campaign_view.get("price_review_required"))
    if "report_restricted" in campaign_view:
        report_restricted = bool(campaign_view.get("report_restricted"))
    else:
        report_restricted = bool(campaign_view.get("safe_mode")) and not stale_status
    stale = price_review_required
    can_authorize = bool(campaign_view.get("top_primary_url")) and not stale and not report_restricted
    already_authorized = bool(campaign_view.get("already_authorized"))
    docs = monthly.get("documents") if isinstance(monthly.get("documents"), list) else []
    document_type = str(_value(property_state, snapshot, "document_type") or row.get("document_type") or "NONE").upper()
    document_available = bool(campaign_view.get("document_available")) and document_type in {"COMMUNAL_MARKET_REPORT", "INDIVIDUAL_APPRAISAL"}
    document_url = campaign_view.get("report_url") if document_available else ""
    commune_label = _text(
        property_state.get("commune") or monthly.get("commune")
        or snapshot.get("commune") or row.get("commune")
    )
    property_type_label = _text(
        property_state.get("property_type") or monthly.get("property_type")
        or snapshot.get("property_type") or row.get("property_type")
    )
    property_region_label = _text(
        property_state.get("region") or monthly.get("region")
        or snapshot.get("region") or row.get("region") or campaign_view.get("region")
    )
    master_identity: dict[str, str] = {}
    if not commune_label or not property_type_label or not property_region_label:
        master_identity = _verified_master_identity(db, property_code)
        commune_label = commune_label or master_identity.get("commune", "")
        property_type_label = property_type_label or master_identity.get("property_type", "")
        property_region_label = property_region_label or master_identity.get("region", "")
    context_operation = (
        _value(property_state, snapshot, "operation") or snapshot.get("operation_resolved")
        or row.get("operation") or campaign_view.get("operation")
    )
    context_price = _value(property_state, snapshot, "current_price")
    context_condition = (
        property_state.get("property_condition") or property_state.get("condition")
        or monthly.get("property_condition") or snapshot.get("property_condition")
    )
    context = _normalize_market_context(
        raw_context, operation=context_operation, region=property_region_label,
        commune=commune_label, property_price_uf=context_price, property_condition=context_condition,
    )
    document_commune_label = commune_label or _text(campaign_view.get("commune"))
    support_documents: list[dict[str, str]] = []
    support_reference_url = document_url or campaign_view.get("advisor_url") or campaign_view.get("top_advisor_url") or ""
    seen_document_urls: set[str] = set()
    if document_url:
        primary_metadata = next((
            item for item in docs
            if isinstance(item, Mapping)
            and item.get("verified")
            and str(item.get("document_type") or item.get("type") or "").upper() == document_type
        ), {})
        support_documents.append(_support_document_item(
            document_type, property_code, document_commune_label, str(document_url), primary_metadata,
        ))
        seen_document_urls.add(str(document_url))
    if not stale and support_reference_url:
        for item in docs:
            if not isinstance(item, Mapping) or not item.get("verified"):
                continue
            extra_type = str(item.get("document_type") or item.get("type") or "").upper()
            extra_url = str(item.get("url") or item.get("document_url") or item.get("report_url") or "")
            if (extra_type not in {"COMMUNAL_MARKET_REPORT", "INDIVIDUAL_APPRAISAL"}
                    or not extra_url or extra_url in seen_document_urls
                    or not _support_document_link_is_safe(
                        extra_url, support_reference_url,
                        campaign_id=str(row.get("campaign_id") or ""),
                        property_code=property_code,
                        owner_email=owner_email,
                        document_type=extra_type,
                    )):
                continue
            support_documents.append(_support_document_item(
                extra_type, property_code, document_commune_label, extra_url, item,
            ))
            seen_document_urls.add(extra_url)

    # Resolve only this property's exact-code PDF to enrich an unstructured
    # appraisal. Never expose Drive URLs; an existing signed document link is
    # retained and reused instead of issuing a duplicate link.
    has_appraisal_document = any(
        item.get("type") == "INDIVIDUAL_APPRAISAL" for item in support_documents
    )
    appraisal_access_expiry = (row.get("portal_access") or {}).get("expires_at")
    appraisal_campaign_id = str(row.get("campaign_id") or "")
    appraisal_access_is_valid = bool(
        isinstance(appraisal_access_expiry, datetime)
        and _as_utc(appraisal_access_expiry) > datetime.now(timezone.utc)
        and appraisal_campaign_id and owner_email
    )
    resolved_appraisal_analysis: Mapping[str, Any] | None = None
    if not stale and property_code and appraisal_access_is_valid:
        try:
            from campanas.private_report import (
                resolve_appraisal_analysis_cached,
                resolve_appraisal_document_cached,
            )

            resolved_appraisal = resolve_appraisal_document_cached(property_code)
            file_record = resolved_appraisal.get("document") if isinstance(resolved_appraisal, Mapping) else None
            if (
                isinstance(resolved_appraisal, Mapping)
                and resolved_appraisal.get("status") == "FOUND"
                and isinstance(file_record, Mapping)
                and file_record.get("id")
            ):
                resolved_appraisal_analysis = resolve_appraisal_analysis_cached(property_code, file_record)
                _log_owner_appraisal_analysis(
                    property_code, resolved_appraisal.get("status"), resolved_appraisal_analysis,
                    error_type=_text(resolved_appraisal.get("error_type")),
                )
                expiry = appraisal_access_expiry
                campaign_id = appraisal_campaign_id
                source = str(campaign_view.get("source") or "").upper()
                source = source if source in {"EMAIL", "WHATSAPP"} else None
                if (
                    not has_appraisal_document
                    and
                    isinstance(expiry, datetime)
                    and _as_utc(expiry) > datetime.now(timezone.utc)
                    and campaign_id and owner_email
                ):
                    from campanas.owner_campaign_live_events import issue_live_token

                    token = issue_live_token(
                        campaign_id=campaign_id,
                        property_code=property_code,
                        action="ver_informe",
                        recipient=owner_email,
                        document_type="INDIVIDUAL_APPRAISAL",
                        expires_at=int(_as_utc(expiry).timestamp()),
                        source=source,
                        interaction_surface="OWNER_PORTAL",
                        cta_placement="ORIGINAL",
                    )
                    origin = urlsplit(str(
                        campaign_view.get("report_url")
                        or campaign_view.get("advisor_url")
                        or campaign_view.get("top_advisor_url") or ""
                    ))
                    base = f"{origin.scheme}://{origin.netloc}" if origin.scheme and origin.netloc else ""
                    appraisal_url = f"{base}/campana/informe?{urlencode({'token': token})}"
                    appraisal_metadata = {
                        "file_modified_at": file_record.get("modifiedTime"),
                    }
                    if resolved_appraisal_analysis.get("appraisal_date"):
                        appraisal_metadata["appraisal_date"] = resolved_appraisal_analysis["appraisal_date"]
                    support_documents.append(_support_document_item(
                        "INDIVIDUAL_APPRAISAL", property_code,
                        document_commune_label, appraisal_url, appraisal_metadata,
                    ))
                    seen_document_urls.add(appraisal_url)
            else:
                _log_owner_appraisal_analysis(
                    property_code,
                    resolved_appraisal.get("status") if isinstance(resolved_appraisal, Mapping) else "ERROR",
                    error_type=_text(resolved_appraisal.get("error_type")) if isinstance(resolved_appraisal, Mapping) else "",
                )
        except Exception as exc:
            # Drive/token failures must never take down the owner portal.
            _log_owner_appraisal_analysis(property_code, "ERROR", error_type=type(exc).__name__)

    diagnosis = _text(recommendation.get("diagnosis")) or (evidence.get("diagnostic-copy") or [None])[0]
    recommendation_text = _text(recommendation.get("text") or recommendation.get("recommendation_text")) or (evidence.get("recommendation-copy") or [None])[0] or _text(campaign_view.get("recommendation_reason"))
    if stale:
        diagnosis = ""
        recommendation_text = ""
    elif not diagnosis and all(activity.get(key) is None for key in ("leads", "total_leads", "conversations", "visits")):
        diagnosis = "No hay actividad comercial suficiente disponible para describir el comportamiento reciente de esta propiedad."
    adjustment_value = _value(recommendation, snapshot, "recommended_adjustment_pct")
    if adjustment_value is None:
        adjustment_value = campaign_view.get("recommended_adjustment_pct")
    adjustment_label = ""
    telemetry_adjustment_pct = None
    if not stale and adjustment_value is not None:
        try:
            adjustment_number = float(str(adjustment_value).replace(",", "."))
            telemetry_adjustment_pct = adjustment_number
            adjustment_label = f"-{abs(adjustment_number):g}%"
        except (TypeError, ValueError):
            raw_adjustment = str(adjustment_value).replace("%", "").replace(",", ".").strip()
            try:
                telemetry_adjustment_pct = float(raw_adjustment)
                adjustment_label = _text(adjustment_value)
            except (TypeError, ValueError):
                adjustment_label = _text(adjustment_value)

    operation = _text(_value(property_state, snapshot, "operation") or snapshot.get("operation_resolved") or row.get("operation") or campaign_view.get("operation"))
    market_reference_card = None
    communal_document = next((
        item for item in support_documents
        if item.get("type") == "COMMUNAL_MARKET_REPORT"
    ), None)
    if communal_document:
        market_reference_card = _communal_market_card(
            db, commune=commune_label, property_type=property_type_label,
            operation=operation, document_url=str(communal_document.get("url") or ""),
        )
    communal_position_reference = dict(communal)
    if isinstance(market_reference_card, Mapping) and market_reference_card.get("kind") == "COMMUNAL_MARKET_REPORT":
        canonical_communal_value = _number_from_label(market_reference_card.get("reference_value_uf_m2"))
        if canonical_communal_value is not None and canonical_communal_value > 0:
            communal_position_reference.update({
                "offer_uf_m2": canonical_communal_value,
                "reference_unit": market_reference_card.get("reference_unit") or "UF/m²",
                "currency_basis": market_reference_card.get("currency_basis") or "UF",
                "source_date": market_reference_card.get("source_date") or communal_position_reference.get("source_date"),
            })
            if market_reference_card.get("area_basis"):
                communal_position_reference["area_basis"] = market_reference_card["area_basis"]
    current_price = _value(property_state, snapshot, "current_price")
    recommended_price = recommendation.get("recommended_price")
    if recommended_price is None:
        recommended_price = snapshot.get("recommended_price")
    if current_price is not None or recommended_price is not None:
        # Use the exact client formatter already used by the campaign renderer.
        from .campaign import _format_client_price
        current_price_label = _format_client_price(current_price, operation) if current_price is not None else campaign_view.get("current_price_label")
        recommended_price_label = _format_client_price(recommended_price, operation) if recommended_price is not None else campaign_view.get("recommended_price_label")
    else:
        current_price_label = campaign_view.get("current_price_label")
        recommended_price_label = campaign_view.get("recommended_price_label")

    gap_explanation = "" if stale else _gap_explanation(position, adjustment_value)
    from .executive import resolve_executive_contact
    executive = resolve_executive_contact(db, row, monthly, _text(campaign_view.get("executive_name")))
    whatsapp_urls = {"TOP": "", "STICKY": ""}
    access_expiry = (row.get("portal_access") or {}).get("expires_at")
    if executive["phone_digits"] and isinstance(access_expiry, datetime) and _as_utc(access_expiry) > datetime.now(timezone.utc):
        from campanas.owner_campaign_live_events import issue_live_token
        origin = urlsplit(str(campaign_view.get("advisor_url") or campaign_view.get("top_advisor_url") or ""))
        base = f"{origin.scheme}://{origin.netloc}" if origin.scheme and origin.netloc else ""
        if base:
            for placement in ("TOP", "STICKY"):
                token = issue_live_token(
                    campaign_id=str(row["campaign_id"]), property_code=property_code,
                    action="executive_whatsapp_clicked", recipient=owner_email,
                    expires_at=int(_as_utc(access_expiry).timestamp()), source=campaign_view.get("source"),
                    interaction_surface="OWNER_PORTAL", cta_placement=placement,
                )
                whatsapp_urls[placement] = f"{base}/owner-portal/executive-whatsapp?{urlencode({'token': token})}"
    current_period = _as_period(monthly.get("period") or monthly.get("generated_at"))
    report_period = current_period or _as_period(snapshot.get("prepared_at")) or _as_period(campaign_snapshot.get("prepared_at")) or datetime.now(timezone.utc).strftime("%Y-%m")
    snapshot_hash = str(monthly.get("snapshot_hash") or monthly.get("content_sha256") or "").strip()
    if not snapshot_hash:
        try:
            snapshot_hash = hashlib.sha256(json.dumps(
                dict(monthly or snapshot), sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            snapshot_hash = hashlib.sha256(f"{property_code}|{report_period}".encode("utf-8")).hexdigest()
    monthly_changes = _previous_snapshot_changes(monthly_snapshots, monthly) if current_period else []
    recommendation_period_label = _recommendation_period_label(monthly.get("period") or monthly.get("generated_at"))
    adjustment_headline_label = ""
    if adjustment_value is not None:
        try:
            headline_adjustment = abs(float(str(adjustment_value).replace("%", "").replace(",", ".")))
            if headline_adjustment == headline_adjustment and headline_adjustment != float("inf"):
                adjustment_headline_label = f"{headline_adjustment:g}%"
        except (TypeError, ValueError):
            pass
    recommended_price_display_label, recommendation_difference_label, recommended_price_display_value = _price_presentation(
        recommended_price, recommended_price_label, operation, current_price,
    )
    recommendation_summary, recommendation_details = _recommendation_narrative(position, activity, adjustment_value)
    if stale:
        adjustment_headline_label = ""
        recommendation_summary = ""
        recommendation_details = []
    appraisal_card = _appraisal_card(
        monthly=monthly, snapshot=snapshot, campaign_view=campaign_view,
        docs=docs, support_documents=support_documents,
        property_code=property_code, operation=operation,
        current_price=current_price, recommended_price=recommended_price,
        extracted_appraisal=resolved_appraisal_analysis,
    )
    historical_email_comparable = False
    simulation_comparables = comparable_state
    simulation_property_state = property_state
    simulation_current_price = current_price
    simulation_recommended_price = recommended_price
    simulation_adjustment = adjustment_value
    simulation_current_price_label = current_price_label
    simulation_recommended_price_label = recommended_price_label
    row_status = str(row.get("send_status") or "").upper()
    campaign_comparable_mode = str(snapshot.get("comparable_mode") or row.get("comparable_mode") or "").upper()
    if (
        not simulation_comparables
        and isinstance(email_html, str) and email_html.strip()
        and row_status in {"SENT", "DELIVERY_UNKNOWN"}
        and campaign_comparable_mode == "PRIMARY_COMPARABLES"
        and not stale
    ):
        surfaces = evidence.get("verified_property_surfaces_m2")
        surfaces = surfaces if isinstance(surfaces, Mapping) else {}
        unit_key = _position_unit_key(position.get("unit"))
        raw_surface_label = _text(position.get("unit")).casefold().replace("ú", "u")
        if "util" in raw_surface_label and ("construid" in raw_surface_label or "built" in raw_surface_label):
            surface_basis = "comparable_surface_ref"
        else:
            surface_basis = "useful" if "util" in unit_key or "útil" in unit_key else "built" if "const" in unit_key else "land" if "terreno" in unit_key else ""
        surface_ref_m2 = surfaces.get(surface_basis) if surface_basis else None
        snapshot_count = _number_from_label(snapshot.get("comparable_count"))
        email_count = _number_from_label(position.get("count"))
        count_consistent = not (snapshot_count is not None and email_count is not None and int(snapshot_count) != int(email_count))
        frozen_count = int(snapshot_count if snapshot_count is not None else email_count or 0)
        frozen_current_price = snapshot.get("current_price")
        frozen_recommended_price = snapshot.get("recommended_price")
        frozen_adjustment = snapshot.get("recommended_adjustment_pct")
        if (
            surface_ref_m2 is not None and frozen_count >= 5 and count_consistent
            and position.get("reference_value") and position.get("property_value")
            and frozen_current_price is not None and frozen_recommended_price is not None
            and frozen_adjustment is not None
        ):
            # Wave1 did not persist a full comparable model in its monthly
            # snapshot. Use only the verified exact sent-email artifact plus
            # the campaign's immutable prices/mode; never fetch current comps.
            simulation_comparables = {
                "source": "VERIFIED_SENT_EMAIL",
                "campaign_comparable_mode": campaign_comparable_mode,
                "evidence_level": "VERIFIED_SENT_EMAIL",
                "positioning_mode": "PRICE_M2",
                "count": frozen_count,
                "reference_value": position.get("reference_value"),
                "property_value": position.get("property_value"),
                "surface_ref_m2": surface_ref_m2,
                "unit": position.get("unit"),
            }
            simulation_property_state = {
                **property_state,
                "current_price": frozen_current_price,
                "operation": operation,
                "surface_ref_m2": surface_ref_m2,
            }
            # Simulation uses the campaign's immutable prices and adjustment,
            # even if the portal later receives a newer monthly price snapshot.
            simulation_current_price = frozen_current_price
            simulation_recommended_price = frozen_recommended_price
            simulation_adjustment = frozen_adjustment
            from .campaign import _format_client_price
            simulation_current_price_label = _format_client_price(frozen_current_price, operation)
            simulation_recommended_price_label = _format_client_price(frozen_recommended_price, operation)
            historical_email_comparable = True
    if historical_email_comparable:
        canonical_area_model = _verified_master_comparable_area_semantics(db, property_code)
        if canonical_area_model:
            simulation_comparables = {**simulation_comparables, **canonical_area_model}
            position["owner_unit"] = _comparable_owner_unit_label(simulation_comparables)
        else:
            # The old email label can be more specific than the canonical
            # model evidence. Keep its numeric comparison, but suppress that
            # unsupported area suffix when the model cannot be matched.
            position["owner_unit"] = "UF/m²"
    position_simulation = _position_simulation_data(
        position=position,
        comparables=simulation_comparables,
        communal=communal_position_reference,
        property_state=simulation_property_state,
        current_price=simulation_current_price,
        recommended_price=simulation_recommended_price,
        current_price_label=simulation_current_price_label,
        recommended_price_label=simulation_recommended_price_label,
        adjustment=simulation_adjustment,
        recommendation_is_monthly=recommendation.get("recommended_price") is not None or historical_email_comparable,
        stale=stale,
    )
    historical_email_verified = bool(
        not stale and row_status in {"SENT", "DELIVERY_UNKNOWN"}
        and isinstance(email_html, str) and email_html.strip()
    )
    static_comparables = comparable_state or (simulation_comparables if historical_email_comparable else {
        "source": "VERIFIED_SENT_EMAIL",
        "campaign_comparable_mode": campaign_comparable_mode,
    })
    position_static = _static_position_data(
        position=position,
        comparables=static_comparables,
        historical_email_verified=historical_email_verified,
        campaign_comparable_mode=campaign_comparable_mode,
        stale=stale,
    )
    market_position = _market_position_data(
        position_simulation=position_simulation,
        position_static=position_static,
        comparables=simulation_comparables,
        communal=communal_position_reference,
        appraisal_card=appraisal_card,
        property_state=simulation_property_state,
        property_code=property_code,
        current_price=simulation_current_price,
        recommended_price=simulation_recommended_price,
        recommendation_is_monthly=recommendation.get("recommended_price") is not None or historical_email_comparable,
        stale=stale,
    )
    market_evidence = _market_evidence_model(
        market_position=market_position,
        position_static=position_static,
        appraisal_card=appraisal_card,
        communal=communal_position_reference,
        stale=stale,
    )
    market_position = {
        **market_position,
        "market_evidence": market_evidence,
        "summary_position_label": market_evidence["summary_position_label"],
        "summary_position_note": market_evidence["summary_position_note"],
    }
    # Keep the existing simulation contract while exposing the independently
    # validated markers from the unified scale. The visual remains read-only.
    market_reference_x = {
        ref.get("kind"): ref.get("x")
        for ref in market_position.get("references", [])
        if isinstance(ref, Mapping)
    } if market_position.get("available") else {}
    position_simulation = {
        **position_simulation,
        "appraisal_x": market_reference_x.get("APPRAISAL"),
        "median_x": market_reference_x.get("COMPARABLE"),
        "communal_x": market_reference_x.get("COMMUNAL"),
        "communal_on_same_scale": "COMMUNAL" in market_reference_x,
        "current_x": market_position.get("current_x") if market_position.get("available") else None,
        "proposed_x": market_position.get("proposed_x") if market_position.get("available") else None,
    }
    publication_presence = resolve_property_publication_presence(db, property_code)
    if isinstance(context, Mapping):
        context = {**context, "narrative": context.get("summary", "")}

    return {
        "logo_url": campaign_view.get("logo_url"),
        "property_code": property_code,
        "property_image_url": (property_media or {}).get("hero_image_url"),
        "property_public_page_url": (property_media or {}).get("public_page_url"),
        "property_public_page_active": bool((property_media or {}).get("public_page_active")),
        "property_image_source": (property_media or {}).get("image_source"),
        "property_type": property_type_label,
        "commune": commune_label,
        "operation": operation,
        "current_price_label": current_price_label if not stale else "",
        "current_price_count_value": current_price if not stale else None,
        "recommended_price_label": recommended_price_label if not stale else "",
        "recommended_price_display_label": recommended_price_display_label if not stale else "",
        "recommended_price_count_value": recommended_price_display_value if not stale else None,
        "recommendation_difference_label": recommendation_difference_label if not stale else "",
        "adjustment_pct": adjustment_value,
        "telemetry_adjustment_pct": telemetry_adjustment_pct,
        "adjustment_label": adjustment_label,
        "adjustment_headline_label": adjustment_headline_label,
        "recommendation_period_label": recommendation_period_label,
        "recommendation_summary": recommendation_summary,
        "recommendation_details": recommendation_details,
        "comparable_count": position.get("count") if position.get("count") is not None else (_value(comparable_state, snapshot, "comparable_count") if _value(comparable_state, snapshot, "comparable_count") is not None else campaign_view.get("comparable_count")),
        "position": position,
        "position_simulation": position_simulation,
        "position_static": position_static,
        "market_position": market_position,
        "market_evidence": market_evidence,
        "appraisal_card": appraisal_card,
        "gap_explanation": gap_explanation,
        "communal_reference": communal,
        "market_reference_card": market_reference_card,
        "market_context": context if isinstance(context, Mapping) else None,
        "activity_90d": {
            "leads": activity.get("leads"),
            "conversations": activity.get("conversations"),
            "visits": activity.get("visits"),
            "summary": _owner_activity_summary(activity.get("summary")),
            "state": activity.get("state"),
            "source": activity.get("source"),
            "integrity": activity.get("integrity"),
            "window_start": activity.get("window_start"),
            "window_end": activity.get("window_end"),
            "source_date": _source_date_label(activity.get("window_end") or activity.get("source_date")),
            "source_label": "Período al" if activity.get("window_end") else ("Actualizado al" if activity.get("source_date") else ""),
            "funnel": commercial_funnel,
        },
        "property_publication_presence": publication_presence,
        "comparables_source_date": _source_date_label(comparable_state.get("source_date") or comparable_state.get("cutoff_date")),
        "diagnosis": diagnosis,
        "recommendation_text": recommendation_text,
        "recommendation_title": _text(recommendation.get("title")) or (evidence.get("recommendation-title") or ["Ajuste de precio sugerido"])[0],
        "document_available": document_available,
        "document_url": document_url,
        "document_type": document_type,
        "document_label": "Tasación individual" if document_type == "INDIVIDUAL_APPRAISAL" else "Informe de mercado comunal" if document_type == "COMMUNAL_MARKET_REPORT" else "",
        "additional_documents": [item for item in docs if isinstance(item, Mapping) and item.get("verified")],
        "support_documents": support_documents,
        "executive_name": executive["name"],
        "executive_email": executive["email"],
        "executive_phone": executive["phone"],
        "executive_photo_url": executive["photo_url"],
        "executive_initials": executive["initials"],
        "executive_role": executive["role"],
        "top_whatsapp_url": whatsapp_urls["TOP"],
        "sticky_whatsapp_url": whatsapp_urls["STICKY"],
        "updated_label": updated_label,
        "data_period": _as_period(monthly.get("period") or monthly.get("generated_at")) or _as_period(snapshot.get("prepared_at")),
        "report_period": report_period,
        "snapshot_hash": snapshot_hash,
        "source": campaign_view.get("source"),
        "safe_mode": stale,
        "price_review_required": price_review_required,
        "report_restricted": report_restricted,
        "already_authorized": already_authorized,
        "can_authorize": can_authorize,
        "top_primary_url": campaign_view.get("top_primary_url") if can_authorize else "",
        "top_advisor_url": campaign_view.get("top_advisor_url") or campaign_view.get("advisor_url"),
        "advisor_url": campaign_view.get("advisor_url"),
        "sticky_primary_url": campaign_view.get("sticky_primary_url") if can_authorize else "",
        "sticky_advisor_url": campaign_view.get("sticky_advisor_url") or campaign_view.get("advisor_url"),
        "market_context_heading": _text((context or {}).get("reference_month")) if isinstance(context, Mapping) else "",
        "historic_source": "MONTHLY_SNAPSHOT" if monthly else "CAMPAIGN_SNAPSHOT",
        "monthly_changes": monthly_changes,
        "previous_month_available": bool(monthly_changes),
        "monthly_unchanged": bool(monthly_changes) and all(item["before"] == item["after"] for item in monthly_changes),
    }
