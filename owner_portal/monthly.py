"""Read-only monthly owner-portal view assembled from verified snapshots.

The campaign ledger and sent-email artifacts are immutable historical sources.
New monthly records may be stored in ``owner_property_portals`` without
changing either source or the registered portal access tokens.
"""

from __future__ import annotations

import hashlib
import html
import re
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
    return {"type": document_type, "title": title, "metadata": " · ".join(parts), "url": url}


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


def _position_gap(reference: Any, subject: Any, unit: str = "") -> float | None:
    """A display-only ratio, only for positively identified compatible units."""
    def units(value: Any) -> str:
        raw = _text(value).casefold().replace("m2", "m²").replace(" ", "")
        match = re.search(r"(uf|clp|\$)/m²(?:útil|util|construid[oa]|total)?", raw)
        return match.group(0).replace("util", "útil") if match else ""
    left, right = units(reference), units(subject)
    # An explicit shared unit binds bare numeric values; conflicting labels fail closed.
    shared = units(unit)
    left = left or shared
    right = right or shared
    if not left or left != right:
        return None
    ref, value = _number_from_label(reference), _number_from_label(subject)
    if ref is None or value is None or ref <= 0 or value < 0:
        return None
    return (value / ref - 1) * 100


def _position_data(evidence: Mapping[str, Any], monthly: Mapping[str, Any]) -> dict[str, Any]:
    comparable = monthly.get("comparables") if isinstance(monthly.get("comparables"), Mapping) else {}
    labels = evidence.get("position-anchor-label", [])
    labels = labels if isinstance(labels, list) else []
    reference_boxes = evidence.get("position-ref-box", [])
    property_boxes = evidence.get("position-prop-box", [])
    reference = _text(comparable.get("reference_value")) or (reference_boxes[0] if reference_boxes else (labels[0] if labels else ""))
    subject = _text(comparable.get("property_value")) or (property_boxes[0] if property_boxes else (labels[1] if len(labels) > 1 else ""))
    if isinstance(comparable.get("reference_value"), (int, float)):
        reference = f"{comparable['reference_value']:g}".replace(".", ",") + " " + _text(comparable.get("unit"))
    if isinstance(comparable.get("property_value"), (int, float)):
        subject = f"{comparable['property_value']:g}".replace(".", ",") + " " + _text(comparable.get("unit"))
    reference = re.sub(r"^Referencia de mercado\s*", "", reference, flags=re.IGNORECASE)
    subject = re.sub(r"^Tu propiedad\s*", "", subject, flags=re.IGNORECASE)
    count = comparable.get("count")
    if count is None:
        count = (evidence.get("evidence-badge-single") or [None])[0]
    if isinstance(count, str):
        count = int(_number_from_label(count)) if _number_from_label(count) is not None else count
    ref_number = _number_from_label(reference)
    subject_number = _number_from_label(subject)
    marker_pct = None
    gap_pct = _position_gap(reference, subject, _text(comparable.get("unit")))
    if gap_pct is not None:
        # Display-only chart coordinate; no recommendation/pricing is derived here.
        marker_pct = max(4.0, min(96.0, 50.0 + gap_pct * 1.25))
    return {
        "count": count,
        "reference_value": reference or None,
        "property_value": subject or None,
        "unit": _text(comparable.get("unit")) or ("UF/m² útil" if ref_number is not None else ""),
        "interpretation": _text(comparable.get("positioning")) or (evidence.get("comparable-summary-single") or [None])[0],
        "marker_pct": marker_pct,
        "marker_label_pct": max(25.0, min(75.0, marker_pct)) if marker_pct is not None else None,
        "gap_pct": gap_pct,
        "gap_label": f"{gap_pct:+.0f}%" if gap_pct is not None else "",
        "gap_note": "sobre referencia" if gap_pct is not None and gap_pct > 0 else "bajo referencia" if gap_pct is not None and gap_pct < 0 else "en referencia",
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
        updated_label = _text(prepared_at)[:10] or "Fecha no disponible"

    status = str(row.get("send_status") or "").upper()
    stale = bool(campaign_view.get("safe_mode")) or status == "SKIPPED_STALE_OR_MISMATCH"
    can_authorize = bool(campaign_view.get("top_primary_url")) and not stale
    already_authorized = bool(campaign_view.get("already_authorized"))
    docs = monthly.get("documents") if isinstance(monthly.get("documents"), list) else []
    document_type = str(_value(property_state, snapshot, "document_type") or row.get("document_type") or "NONE").upper()
    document_available = bool(campaign_view.get("document_available")) and document_type in {"COMMUNAL_MARKET_REPORT", "INDIVIDUAL_APPRAISAL"}
    document_url = campaign_view.get("report_url") if document_available and not stale else ""
    commune_label = _text(_value(property_state, snapshot, "commune") or row.get("commune") or campaign_view.get("commune"))
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
            document_type, property_code, commune_label, str(document_url), primary_metadata,
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
                extra_type, property_code, commune_label, extra_url, item,
            ))
            seen_document_urls.add(extra_url)

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

    return {
        "logo_url": campaign_view.get("logo_url"),
        "property_code": property_code,
        "property_image_url": (property_media or {}).get("hero_image_url"),
        "property_public_page_url": (property_media or {}).get("public_page_url"),
        "property_public_page_active": bool((property_media or {}).get("public_page_active")),
        "property_image_source": (property_media or {}).get("image_source"),
        "property_type": _text(_value(property_state, snapshot, "property_type") or row.get("property_type") or campaign_view.get("property_type") or evidence.get("property_type")),
        "commune": _text(_value(property_state, snapshot, "commune") or row.get("commune") or campaign_view.get("commune") or evidence.get("commune")),
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
        "gap_explanation": gap_explanation,
        "communal_reference": communal,
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
