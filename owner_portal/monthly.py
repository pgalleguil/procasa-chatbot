"""Read-only monthly owner-portal view assembled from verified snapshots.

The campaign ledger and sent-email artifacts are immutable historical sources.
New monthly records may be stored in ``owner_property_portals`` without
changing either source or the registered portal access tokens.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

from .property_media import verified_historical_media, verified_media_for_property


OWNER_PROPERTY_PORTAL_COLLECTION = "owner_property_portals"
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
    for key in ("source_date", "issued_at", "issued_on", "issue_date", "document_date"):
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
                           campaign_view: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return property-scoped appraisal maps in the approved source priority."""
    sources: list[Mapping[str, Any]] = []
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


def _appraisal_card(*, monthly: Mapping[str, Any], snapshot: Mapping[str, Any],
                    campaign_view: Mapping[str, Any], docs: list[Any], support_documents: list[dict[str, str]],
                    property_code: str, operation: str, current_price: Any,
                    recommended_price: Any) -> dict[str, Any] | None:
    """Build a verified appraisal view; PDF existence alone never implies a value."""
    appraisal_docs = [item for item in support_documents if item.get("type") == "INDIVIDUAL_APPRAISAL"]
    # Multiple distinct verified appraisal documents for a property are ambiguous.
    urls = {item.get("url") for item in appraisal_docs if item.get("url")}
    if len(urls) > 1:
        return None
    document = appraisal_docs[0] if len(appraisal_docs) == 1 else None

    appraisal: Mapping[str, Any] | None = None
    for candidate in _appraisal_source_maps(monthly, snapshot, campaign_view):
        code = _text(candidate.get("property_code") or candidate.get("codigo"))
        if code and code != property_code:
            continue
        if candidate.get("verified") is not True and candidate.get("source_verified") is not True:
            continue
        if any(_strict_appraisal_number(candidate.get(key)) is not None for key in (
            "estimated_low_uf", "estimated_mid_uf", "estimated_high_uf",
        )):
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
        return {
            "mode": "DOCUMENT_ONLY", "title": title,
            "subtitle": "Existe una tasación individual asociada específicamente a esta propiedad.",
            "issued_label": "",
            "copy": "Puedes revisar el documento completo para conocer sus antecedentes, metodología y referencia de valor.",
            "metrics": [], "markers": [], "interpretation": "", "details": [],
            "method_copy": method_copy, "document_url": document_url,
            "document_title": "Tasación individual", "document_metadata": document_metadata,
            "property_code": property_code,
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
    stored_position = _text(appraisal.get("position_vs_appraisal")).upper()
    conflict = bool(
        current is not None and low is not None and high is not None and (
            (stored_position == "ABOVE_RANGE" and current <= high)
            or (stored_position == "WITHIN_RANGE" and not low <= current <= high)
            or (stored_position == "BELOW_RANGE" and current >= low)
        )
    )

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
            if not conflict:
                if current > high:
                    pct = f"{abs(gap_pct):.1f}%".replace(".", ",") if gap_pct is not None else ""
                    interpretation = "El precio publicado se encuentra por encima del rango de referencia de la tasación." + (f" Frente al valor central estimado, la diferencia es aproximadamente {pct}." if pct else "")
                elif current < low:
                    interpretation = "El precio publicado se encuentra por debajo del rango de referencia de la tasación."
                else:
                    interpretation = "El precio publicado se encuentra dentro del rango estimado por la tasación."
        elif mid is not None and gap_pct is not None:
            direction = "sobre" if gap_pct > 0 else "bajo" if gap_pct < 0 else "en línea con"
            interpretation = f"El precio publicado se encuentra aproximadamente {abs(gap_pct):.1f}% {direction} el valor de referencia de la tasación.".replace(".", ",", 1) if gap_pct != 0 else "El precio publicado se encuentra en línea con el valor de referencia de la tasación."
    date_label = _appraisal_date_label(appraisal, docs)
    details = []
    for label, value in (("Rango inferior", low), ("Valor central", mid),
                         ("Rango superior", high), ("Precio publicado", current),
                         ("Con ajuste recomendado", recommended)):
        if value is None:
            continue
        details.append({"label": label, "value": price_label(value)})
    if stored_position and not conflict:
        if stored_position in {"ABOVE_RANGE", "WITHIN_RANGE", "NEAR_RANGE", "BELOW_RANGE"}:
            details.append({"label": "Posición frente al rango", "value": historical_model.get("position_label") or "Referencia disponible"})
    if date_label:
        details.append({"label": "Fecha de tasación", "value": date_label})
    methodology = _text(appraisal.get("methodology") or appraisal.get("method_label") or appraisal.get("method"))
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
        "property_type": _text(metadata.get("tipo_propiedad")),
    }


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
    if re.search(r"\$[\d.,]+/m²/mes", compact):
        return "$/m²/mes"
    if re.search(r"[\d.,]+clp/m²/mes", compact):
        return "CLP/m²/mes"
    match = re.search(r"(uf|clp)/m²(útil|util|construido|construida|total|terreno)?(/mes)?", compact)
    if match:
        currency, basis, monthly = match.groups()
        basis = "útil" if basis in {"util", "útil"} else basis or ""
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
    raw = raw.replace("deoferta", "")
    match = re.fullmatch(r"(uf|clp|\$)/m²(?:útil|util|construido|construida|total|terreno)?", raw)
    return match.group(0).replace("util", "útil") if match else ""


def _position_unit_dimension(value: Any) -> str:
    key = _position_unit_key(value)
    if key.endswith("/mes"):
        return key
    return next((unit for unit in ("uf/m²", "clp/m²", "$/m²") if key.startswith(unit)), "")


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


def _position_data(evidence: Mapping[str, Any], monthly: Mapping[str, Any]) -> dict[str, Any]:
    comparable = monthly.get("comparables") if isinstance(monthly.get("comparables"), Mapping) else {}
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
        "source": selected_source,
        "interpretation": _text(comparable.get("positioning")) or (evidence.get("comparable-summary-single") or [None])[0],
        "marker_pct": marker_pct,
        "marker_label_pct": max(25.0, min(75.0, marker_pct)) if marker_pct is not None else None,
        "gap_pct": gap_pct,
        "gap_label": f"{gap_pct:+.0f}%" if gap_pct is not None else "",
        "gap_note": "sobre referencia" if gap_pct is not None and gap_pct > 0 else "bajo referencia" if gap_pct is not None and gap_pct < 0 else "en referencia",
    }


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

    rounded_gap = int(round(gap))
    if rounded_gap > 0:
        gap_label = f"{rounded_gap:+d}% sobre propiedades similares"
    elif rounded_gap < 0:
        gap_label = f"{rounded_gap:d}% bajo propiedades similares"
    else:
        gap_label = "En línea con propiedades similares"
    return {
        "available": True,
        "count": int(count),
        "source": source,
        "reference_value": reference,
        "property_value": property_value,
        "reference_label": f"{_format_position_value(reference_number)} {unit}",
        "property_label": f"{_format_position_value(property_number)} {unit}",
        "unit": unit,
        "gap_pct": gap,
        "gap_label": gap_label,
    }


def _normalize_market_context(context: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Adapt the verified monthly schema and historical email evidence to the view."""
    if not isinstance(context, Mapping):
        return None
    result = dict(context)
    kpis = result.get("kpis")
    if not isinstance(kpis, list):
        candidates = (
            ("Hipotecario", result.get("mortgage_rate"), result.get("mortgage_note")),
            ("TPM", result.get("tpm"), result.get("tpm_note")),
            ("Demanda", result.get("demand_status"), None),
        )
        kpis = [
            {"label": label, "value": value, "note": note}
            for label, value, note in candidates if value is not None and str(value).strip()
        ]
    has_metric = any(
        isinstance(item, Mapping) and any(_text(item.get(key)) for key in ("value", "note"))
        for item in kpis
    )
    has_metric = has_metric or any(_text(result.get(key)) for key in ("mortgage_rate", "tpm", "demand_status"))
    has_summary = _text(result.get("summary") or result.get("context_summary"))
    if not has_metric and not has_summary:
        return None
    aliases = {
        "hipotecario": ("hipotecario", "financiamiento", "mortgage"),
        "tpm": ("tpm", "tasa de política"),
        "demanda": ("demanda", "demand"),
    }
    normalized_kpis = []
    for label, needles in aliases.items():
        item = next((entry for entry in kpis if isinstance(entry, Mapping) and any(
            needle in _text(entry.get("label")).casefold() for needle in needles
        )), None)
        if not item or item.get("value") is None or not str(item.get("value")).strip() or _text(item.get("value")).casefold() in {"no disponible", "n/a"}:
            continue
        normalized_kpis.append({
            "label": "TPM" if label == "tpm" else label.capitalize(),
            "value": str(item["value"]).strip(),
            "note": _text(item.get("note")) if item else None,
        })
    result["kpis"] = normalized_kpis
    result["reference_month"] = result.get("reference_month") or result.get("period")
    if result.get("source_date"):
        result["source_date"] = _source_date_label(result["source_date"])
    result["summary"] = result.get("summary") or result.get("context_summary")
    sources = result.get("sources") or result.get("source_names")
    if isinstance(sources, (list, tuple)):
        sources = ", ".join(str(item).strip() for item in sources if str(item).strip())
    result["sources"] = re.sub(r"^\s*Fuentes\s*:\s*", "", str(sources), flags=re.IGNORECASE) if sources else None
    return result if result["kpis"] or result.get("summary") else None


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


def _price_presentation(value: Any, exact_label: Any, operation: str, current_value: Any) -> tuple[str, str]:
    """Create presentation-only rounded UF labels; leave stored/exact prices untouched."""
    from .campaign import _format_client_price

    exact = _text(exact_label)
    if not exact.casefold().endswith("uf"):
        return exact, ""
    amount = _number_from_label(value)
    if amount is None or amount <= 0:
        return exact, ""
    increment = Decimal("50") if amount >= 1000 else Decimal("10")
    rounded = (Decimal(str(amount)) / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment
    proposed_label = "≈ " + _format_client_price(float(rounded), operation)
    current_label = _text(_format_client_price(current_value, operation)) if current_value is not None else ""
    current_amount = _number_from_label(current_value)
    difference_label = ""
    if current_label.casefold().endswith("uf") and current_amount is not None and current_amount > float(rounded):
        difference = current_amount - float(rounded)
        difference_label = "≈ " + _format_client_price(difference, operation)
    return proposed_label, difference_label


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
    raw_context = monthly.get("market_context") if isinstance(monthly.get("market_context"), Mapping) else evidence.get("market_context")
    context = _normalize_market_context(raw_context)
    activity = monthly.get("activity_90d") if isinstance(monthly.get("activity_90d"), Mapping) else evidence.get("activity_90d", {})
    activity = activity if isinstance(activity, Mapping) else {}
    if activity.get("leads") is None:
        leads = campaign_view.get("leads_90d")
        if leads is None:
            leads = snapshot.get("leads_90d")
        if leads is not None:
            activity = {**activity, "leads": leads}
    for activity_key in ("conversations", "visits", "summary"):
        if activity.get(activity_key) is None and snapshot.get(activity_key) is not None:
            activity = {**activity, activity_key: snapshot.get(activity_key)}
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
    stale = bool(campaign_view.get("safe_mode")) or status == "SKIPPED_STALE_OR_MISMATCH"
    can_authorize = bool(campaign_view.get("top_primary_url")) and not stale
    already_authorized = bool(campaign_view.get("already_authorized"))
    docs = monthly.get("documents") if isinstance(monthly.get("documents"), list) else []
    document_type = str(_value(property_state, snapshot, "document_type") or row.get("document_type") or "NONE").upper()
    document_available = bool(campaign_view.get("document_available")) and document_type in {"COMMUNAL_MARKET_REPORT", "INDIVIDUAL_APPRAISAL"}
    document_url = campaign_view.get("report_url") if document_available and not stale else ""
    commune_label = _text(
        property_state.get("commune") or monthly.get("commune")
        or snapshot.get("commune") or row.get("commune")
    )
    property_type_label = _text(
        property_state.get("property_type") or monthly.get("property_type")
        or snapshot.get("property_type") or row.get("property_type")
    )
    if not commune_label or not property_type_label:
        master_identity = _verified_master_identity(db, property_code)
        commune_label = commune_label or master_identity.get("commune", "")
        property_type_label = property_type_label or master_identity.get("property_type", "")
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

    # The monthly snapshot is the preferred source. If it has no usable,
    # property-scoped appraisal link, resolve only this property's exact-code
    # PDF through the existing private-report resolver. Never expose Drive URLs.
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
    if not stale and not has_appraisal_document and property_code and appraisal_access_is_valid:
        try:
            from campanas.private_report import resolve_appraisal_document_cached

            resolved_appraisal = resolve_appraisal_document_cached(property_code)
            file_record = resolved_appraisal.get("document") if isinstance(resolved_appraisal, Mapping) else None
            if (
                isinstance(resolved_appraisal, Mapping)
                and resolved_appraisal.get("status") == "FOUND"
                and isinstance(file_record, Mapping)
                and file_record.get("id")
            ):
                expiry = appraisal_access_expiry
                campaign_id = appraisal_campaign_id
                source = str(campaign_view.get("source") or "").upper()
                source = source if source in {"EMAIL", "WHATSAPP"} else None
                if (
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
                    support_documents.append(_support_document_item(
                        "INDIVIDUAL_APPRAISAL", property_code,
                        document_commune_label, appraisal_url, appraisal_metadata,
                    ))
                    seen_document_urls.add(appraisal_url)
        except Exception:
            # Drive/token failures must never take down the owner portal.
            pass

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
    if not stale and adjustment_value is not None:
        try:
            adjustment_number = float(str(adjustment_value).replace(",", "."))
            adjustment_label = f"-{abs(adjustment_number):g}%"
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
    recommended_price_display_label, recommendation_difference_label = _price_presentation(
        recommended_price, recommended_price_label, operation, current_price,
    )
    recommendation_summary, recommendation_details = _recommendation_narrative(position, activity, adjustment_value)
    if stale:
        adjustment_headline_label = ""
        recommendation_summary = ""
        recommendation_details = []
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
    position_simulation = _position_simulation_data(
        position=position,
        comparables=simulation_comparables,
        communal=communal,
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
    static_comparables = comparable_state or {
        "source": "VERIFIED_SENT_EMAIL",
        "campaign_comparable_mode": campaign_comparable_mode,
    }
    position_static = _static_position_data(
        position=position,
        comparables=static_comparables,
        historical_email_verified=historical_email_verified,
        campaign_comparable_mode=campaign_comparable_mode,
        stale=stale,
    )
    appraisal_card = _appraisal_card(
        monthly=monthly, snapshot=snapshot, campaign_view=campaign_view,
        docs=docs, support_documents=support_documents,
        property_code=property_code, operation=operation,
        current_price=current_price, recommended_price=recommended_price,
    )

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
        "recommended_price_label": recommended_price_label if not stale else "",
        "recommended_price_display_label": recommended_price_display_label if not stale else "",
        "recommendation_difference_label": recommendation_difference_label if not stale else "",
        "adjustment_pct": adjustment_value,
        "adjustment_label": adjustment_label,
        "adjustment_headline_label": adjustment_headline_label,
        "recommendation_period_label": recommendation_period_label,
        "recommendation_summary": recommendation_summary,
        "recommendation_details": recommendation_details,
        "comparable_count": position.get("count") if position.get("count") is not None else (_value(comparable_state, snapshot, "comparable_count") if _value(comparable_state, snapshot, "comparable_count") is not None else campaign_view.get("comparable_count")),
        "position": position,
        "position_simulation": position_simulation,
        "position_static": position_static,
        "appraisal_card": appraisal_card,
        "gap_explanation": gap_explanation,
        "communal_reference": communal,
        "market_reference_card": market_reference_card,
        "market_context": context if isinstance(context, Mapping) else None,
        "activity_90d": {
            "leads": activity.get("leads", activity.get("total_leads")),
            "conversations": activity.get("conversations"),
            "visits": activity.get("visits"),
            "summary": _text(activity.get("summary")),
            "source_date": _source_date_label(activity.get("source_date") or activity.get("period")),
            "source_label": "Actualizado al" if activity.get("source_date") else ("Período" if activity.get("period") else ""),
        },
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
        "source": campaign_view.get("source"),
        "safe_mode": stale,
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
