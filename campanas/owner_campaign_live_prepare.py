"""Prepare the first production campaign batch without sending email."""

from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from pymongo import MongoClient

from config import Config
from campanas.owner_campaign_live_config import (
    BOSS_CC,
    EXCLUDED_OWNER_EMAILS,
    MANUAL_CAMPAIGN_EXCLUDED_CODES,
    MANUAL_RECENT_EXCLUSION_CODES,
    MANUAL_RECENT_EXCLUSION_PENDING,
    PRODUCTION_CAMPAIGN_ID,
    WAVE2_AMBIGUOUS_OWNER_CODES,
    WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID,
)
from campanas import owner_campaign_test_runtime as runtime


BASE_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(os.getenv(
    "OWNER_CAMPAIGN_MANIFEST_PATH",
    str(BASE_DIR / "reports" / "owner_campaign_sucre_wave1_20260928_manifest.csv"),
))
MANIFEST_SIZE = 25
DEFAULT_BATCH_ID = os.getenv("OWNER_CAMPAIGN_BATCH_ID", "wave1_full_20260928").strip()
LIVE_MANIFEST_COLUMNS = [
    "batch_id", "property_code", "operation_resolved", "owner_email", "owner_name",
    "boss_cc", "executive_name", "executive_email", "leads_90d",
    "comparables_available", "valuation_available", "document_type",
    "recommendation_reason", "document_conflict", "recommended_adjustment_pct",
    "current_price", "recommended_price", "current_price_clp", "recommended_price_clp",
    "gradual_adjustment_pct", "gradual_price", "gradual_price_clp", "campaign_id", "send_status",
]


def _owner_name(master: dict[str, Any]) -> str:
    for path in (
        "datos_propietario.nombre", "datos_propietario.nombre_completo",
        "propietario.nombre", "owner.name", "contacto.nombre", "nombre_propietario",
    ):
        value = runtime._path(master, path)
        if value and str(value).strip():
            return str(value).strip()
    first = runtime._path(master, "datos_propietario.nombres") or ""
    last = runtime._path(master, "datos_propietario.apellidos") or ""
    return " ".join(str(part).strip() for part in (first, last) if str(part).strip())


def _source_eligible(master: dict[str, Any]) -> bool:
    code = str(master.get("codigo") or "").strip()
    return (
        bool(code)
        and runtime._active_available(master)
        and runtime._is_sucre(master)
        and code not in MANUAL_CAMPAIGN_EXCLUDED_CODES
        and code not in MANUAL_RECENT_EXCLUSION_CODES
    )


def _row_for(db: Any, master: dict[str, Any], lead_percentiles: dict[str, Any], now: datetime) -> dict[str, Any]:
    code = str(master["codigo"]).strip()
    owner_email = runtime._email_from_property(master)
    assigned_name = str(runtime._path(master, "estado.ejecutivo") or "").strip()
    executive = runtime._resolve_executive(db, master)
    if (
        not assigned_name
        or runtime._fold(assigned_name) in runtime.UNKNOWN_EXECUTIVES
        or not executive.get("match_unique")
        or runtime._fold(executive.get("name")) != runtime._fold(assigned_name)
        or not runtime._valid_owner_email(executive.get("email"))
    ):
        raise runtime.LiveTestCaseBuildError("executive_assignment_or_email_unverified")
    if not runtime._valid_owner_email(BOSS_CC):
        raise runtime.LiveTestCaseBuildError("boss_cc_invalid")

    raw_operation = runtime.resolve_property_operation(master)
    operation = (
        runtime.resolve_property_operation(master, requested_operation=runtime.VENTA)
        if raw_operation == runtime.VENTA_ARRIENDO else raw_operation
    )
    if operation not in {runtime.VENTA, runtime.ARRIENDO}:
        raise runtime.LiveTestCaseBuildError("operation_unresolved")
    segment = runtime._campaign_segment(master) or "INSUFFICIENT_EVIDENCE"
    case = runtime._build_case(
        db,
        master,
        "LIVE",
        segment=segment,
        lead_percentiles_by_operation=lead_percentiles,
        requested_operation=operation,
        resolve_image=False,
        now=now,
    )
    model = case.render_context["property_model"]
    recommendation = model.get("pricing_recommendation") or {}
    current = case.current_price
    proposed = runtime._number(recommendation.get("recommended_price"))
    adjustment = runtime._number(recommendation.get("recommended_adjustment_pct"))
    if proposed is None or adjustment is None or not 5 <= adjustment <= 10:
        raise runtime.LiveTestCaseBuildError("required_price_recommendation_missing_or_out_of_range")
    if model.get("recommendation") != "con ajuste de precio sustentado":
        raise runtime.LiveTestCaseBuildError("required_recommendation_section_missing")
    document_type = str(case.document_type or "NONE").upper()
    if model.get("document", {}).get("visible") is not (document_type != "NONE"):
        raise runtime.LiveTestCaseBuildError("document_visibility_mismatch")
    comp = model.get("comparable") or {}
    activity = model.get("activity_90d") or {}
    subject = runtime._number(comp.get("positioning_property_value"))
    ref = runtime._number(comp.get("positioning_reference_value"))
    if comp.get("visible") and subject is not None and ref is not None and subject <= ref:
        reason = "COMMERCIAL_REPOSITIONING_LEADS_90D; below/at comparable reference"
    elif comp.get("visible") and subject is not None and ref is not None and subject > ref:
        reason = "COMMERCIAL_REPOSITIONING_LEADS_90D; comparable reference supports adjustment"
    else:
        reason = "COMMERCIAL_REPOSITIONING_LEADS_90D; comparable unavailable or incompatible"
    if activity.get("lead_total") == 0:
        reason += "; zero unique leads in 90 days"
    elif activity.get("lead_total") is not None:
        reason += f"; {activity['lead_total']} unique leads in 90 days"
    return {
        "property_code": code,
        "operation_resolved": operation,
        "property_type": case.property_type,
        "commune": case.commune,
        "owner_email": owner_email,
        "owner_name": _owner_name(master),
        "boss_cc": BOSS_CC,
        "executive_name": executive["name"],
        "executive_email": str(executive["email"]).casefold(),
        "leads_90d": activity.get("lead_total"),
        "comparables_available": bool(comp.get("visible")),
        "valuation_available": document_type == "INDIVIDUAL_APPRAISAL",
        "document_type": document_type,
        "document_conflict": bool(model.get("document_conflict")),
        "recommendation_reason": reason,
        "recommended_adjustment_pct": int(adjustment),
        "current_price": current,
        "recommended_price": proposed,
        "current_price_clp": model.get("current_price_clp"),
        "recommended_price_clp": model.get("recommended_price_clp"),
        "gradual_adjustment_pct": (model.get("gradual_price_alternative") or {}).get("adjustment_pct"),
        "gradual_price": (model.get("gradual_price_alternative") or {}).get("price"),
        "gradual_price_clp": (model.get("gradual_price_alternative") or {}).get("price_clp"),
        "campaign_id": PRODUCTION_CAMPAIGN_ID,
        "batch_id": DEFAULT_BATCH_ID,
        "send_status": "READY",
        "_model": model,
        "_master": master,
    }


def prepare(*, write_ledger: bool = True) -> dict[str, Any]:
    if not PRODUCTION_CAMPAIGN_ID or "test" in PRODUCTION_CAMPAIGN_ID.casefold():
        raise RuntimeError("Production campaign id is empty or marked as test")
    if not Config.MONGO_URI:
        raise RuntimeError("MONGO_URI is unavailable")
    now = datetime.now(timezone.utc)
    client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=15000)
    try:
        db = client[Config.DB_NAME]
        properties = list(db[runtime.PROPERTY_COLLECTION].find({
            "estado.oficina": runtime.CAMPAIGN_OFFICE,
            "estado.estado_prop360": {"$regex": "^activa$", "$options": "i"},
            "disponible_prop360": True,
        }))
        properties = [item for item in properties if runtime._is_sucre(item) and runtime._active_available(item)]
        by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
        invalid_owner = []
        for master in properties:
            email = runtime._email_from_property(master, required=False)
            if not email or email in EXCLUDED_OWNER_EMAILS:
                invalid_owner.append(master)
                continue
            by_owner[email].append(master)

        single_groups = {email: group[0] for email, group in by_owner.items() if len(group) == 1}
        multiproperty_groups = {email: group for email, group in by_owner.items() if len(group) > 1}
        sale_rent_percentiles = runtime.calculate_owner_campaign_lead_percentiles(db, now=now)
        prepared = []
        rejected = defaultdict(int)
        for email, master in sorted(single_groups.items(), key=lambda pair: str(pair[1].get("codigo") or "")):
            if not _source_eligible(master):
                continue
            try:
                row = _row_for(db, master, sale_rent_percentiles, now)
                prepared.append(row)
            except (runtime.LiveTestCaseBuildError, KeyError, TypeError, ValueError) as exc:
                rejected[getattr(exc, "error_code", str(exc) or type(exc).__name__)] += 1
            if len(prepared) >= MANIFEST_SIZE:
                break

        # Final mail-safe single-owner set; reject duplicate TOs/codes before
        # choosing the deterministic first batch.
        unique = []
        seen_owners: set[str] = set()
        seen_codes: set[str] = set()
        for row in sorted(prepared, key=lambda item: int(item["property_code"]) if item["property_code"].isdigit() else item["property_code"]):
            if row["owner_email"] in seen_owners or row["property_code"] in seen_codes:
                rejected["duplicate_owner_or_property"] += 1
                continue
            seen_owners.add(row["owner_email"])
            seen_codes.add(row["property_code"])
            unique.append(row)
        batch = unique[:MANIFEST_SIZE]
        if len(batch) < MANIFEST_SIZE:
            raise RuntimeError(f"Only {len(batch)} production-safe candidates are available; manifest requires 25")

        if write_ledger:
            ledger = db[Config.COLLECTION_CAMPANAS_LOG]
            for row in batch:
                key = f"{row['campaign_id']}:{row['property_code']}"
                existing = ledger.find_one({"_id": key}, {"campaign_id": 1, "property_code": 1, "send_status": 1})
                if existing and (
                    existing.get("campaign_id") != row["campaign_id"]
                    or str(existing.get("property_code")) != row["property_code"]
                ):
                    raise RuntimeError(f"Campaign ledger key collision for property {row['property_code']}")
                ledger.update_one({"_id": key}, {"$setOnInsert": {
                    "campaign_id": row["campaign_id"],
                    "property_code": row["property_code"],
                    "operation": row["operation_resolved"],
                    "property_type": row["property_type"],
                    "commune": row["commune"],
                    "owner_email": row["owner_email"],
                    "owner_name": row["owner_name"],
                    "executive_name": row["executive_name"],
                    "executive_email": row["executive_email"],
                    "boss_cc": row["boss_cc"],
                    "recommended_adjustment_pct": row["recommended_adjustment_pct"],
                    "current_price": row["current_price"],
                    "current_price_clp": row["current_price_clp"],
                    "recommended_price": row["recommended_price"],
                    "recommended_price_clp": row["recommended_price_clp"],
                    "gradual_adjustment_pct": row["gradual_adjustment_pct"],
                    "gradual_price": row["gradual_price"],
                    "gradual_price_clp": row["gradual_price_clp"],
                    "document_type": row["document_type"],
                    "document_conflict": row["document_conflict"],
                    "send_status": "READY",
                    "created_at": now,
                    "events": [],
                    "authorization_status": "PENDING",
                }}, upsert=True)

        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MANIFEST_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=LIVE_MANIFEST_COLUMNS)
            writer.writeheader()
            writer.writerows({key: row.get(key) for key in LIVE_MANIFEST_COLUMNS} for row in batch)

        for row in batch:
            # The render check uses the final template and real property model;
            # no SMTP call is made here.
            row["rendered_html"] = _render_one(row)
        return {
            "base_count": len(properties),
            "single_owner_properties": sum(len(items) for items in by_owner.values() if len(items) == 1),
            "multi_owner_count": len(multiproperty_groups),
            "multi_property_count": sum(len(items) for items in multiproperty_groups.values()),
            "invalid_owner_count": len(invalid_owner),
            "eligible_count": len(unique),
            "rejected": dict(rejected),
            "batch": batch,
            "campaign_id": PRODUCTION_CAMPAIGN_ID,
            "manifest_path": str(MANIFEST_PATH),
            "manual_recent_exclusion_pending": MANUAL_RECENT_EXCLUSION_PENDING,
            "manual_recent_exclusion_codes": sorted(MANUAL_RECENT_EXCLUSION_CODES),
        }
    finally:
        client.close()


def prepare_manifest_from_selection(
    selection_path: str | Path,
    output_path: str | Path,
    *,
    db: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a complete send manifest from an explicit code/batch selection.

    The selection file is a separate operator-owned artifact. This helper is
    generic and contains no pilot property codes; it performs read-only Mongo
    access and writes only the requested CSV.
    """
    from contextlib import nullcontext

    from campanas.owner_campaign_live_sender import normalize_property_code

    selection_path = Path(selection_path)
    output_path = Path(output_path)
    allowed_campaigns = {PRODUCTION_CAMPAIGN_ID, WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID}
    if any(not campaign or "test" in campaign.casefold() for campaign in allowed_campaigns):
        raise RuntimeError("Production campaign id is empty or marked as test")
    with selection_path.open("r", encoding="utf-8-sig", newline="") as handle:
        selected_rows = list(csv.DictReader(handle))
    if not selected_rows:
        raise RuntimeError("Selection manifest is empty")
    batch_ids = {str(row.get("batch_id") or "").strip() for row in selected_rows}
    campaign_ids = {str(row.get("campaign_id") or "").strip() for row in selected_rows}
    codes = [normalize_property_code(row.get("property_code")) for row in selected_rows]
    if "" in batch_ids or len(batch_ids) != 1 or len(campaign_ids) != 1 or not campaign_ids <= allowed_campaigns:
        raise RuntimeError("Selection manifest campaign or batch is inconsistent")
    campaign_id = next(iter(campaign_ids))
    wave2 = campaign_id == WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID
    if any(not code for code in codes) or len(codes) != len(set(codes)):
        raise RuntimeError("Selection manifest contains invalid or duplicate property codes")
    batch_id = next(iter(batch_ids))

    owned_client = None
    if db is None:
        if not Config.MONGO_URI:
            raise RuntimeError("MONGO_URI is unavailable")
        owned_client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=15000)
        db = owned_client[Config.DB_NAME]
    close_client = owned_client.close if owned_client is not None else nullcontext
    try:
        now = now or datetime.now(timezone.utc)
        query = {
            "estado.oficina": runtime.CAMPAIGN_OFFICE,
            "estado.estado_prop360": {"$regex": "^activa$", "$options": "i"},
            "disponible_prop360": True,
        }
        if wave2:
            query["codigo"] = {
                "$in": [variant for code in codes for variant in runtime._variants(code)]
            }
        properties = [
            item for item in db[runtime.PROPERTY_COLLECTION].find(query)
            if runtime._is_sucre(item) and runtime._active_available(item)
        ]
        by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for master in properties:
            code = normalize_property_code(master.get("codigo"))
            if code:
                by_code[code].append(master)
            email = runtime._email_from_property(master, required=False)
            if email:
                by_owner[email].append(master)
        if any(len(by_code.get(code, [])) != 1 for code in codes):
            raise RuntimeError("Selection includes missing or duplicate active PROCASA SUCRE properties")

        percentile_data = runtime.calculate_owner_campaign_lead_percentiles(db, now=now)
        prepared = []
        banned_emails = {str(email).strip().casefold() for email in EXCLUDED_OWNER_EMAILS}
        banned_codes = {
            normalize_property_code(code)
            for code in (MANUAL_CAMPAIGN_EXCLUDED_CODES | MANUAL_RECENT_EXCLUSION_CODES)
        }
        for code in codes:
            master = by_code[code][0]
            owner_email = runtime._email_from_property(master)
            if code in banned_codes or owner_email.casefold() in banned_emails:
                raise RuntimeError(f"Selection contains an excluded property: {code}")
            if wave2 and code in WAVE2_AMBIGUOUS_OWNER_CODES:
                raise RuntimeError(f"Selection contains an ambiguous Wave 2 property: {code}")
            if not wave2 and len(by_owner.get(owner_email, [])) != 1:
                raise RuntimeError(f"Selection contains a multiproperty owner: {code}")
            row = _row_for(db, master, percentile_data, now)
            if normalize_property_code(row["property_code"]) != code:
                raise RuntimeError("Property identity changed during preparation")
            row.update({"batch_id": batch_id, "campaign_id": campaign_id, "send_status": "READY"})
            prepared.append(row)

        owner_emails = [str(row["owner_email"]).strip().casefold() for row in prepared]
        if not wave2 and len(owner_emails) != len(set(owner_emails)):
            raise RuntimeError("Selection contains duplicate owner recipients")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=LIVE_MANIFEST_COLUMNS)
            writer.writeheader()
            writer.writerows({key: row.get(key) for key in LIVE_MANIFEST_COLUMNS} for row in prepared)
        return {
            "manifest_path": str(output_path),
            "batch_id": batch_id,
            "campaign_id": campaign_id,
            "manifest_count": len(prepared),
            "property_codes": [row["property_code"] for row in prepared],
        }
    finally:
        if owned_client is not None:
            close_client()


def _render_one(row: dict[str, Any]) -> str:
    from analytics.owner_campaign_email_v2 import render_owner_campaign_email_v2
    from campanas.owner_campaign_live_events import issue_live_token

    model = dict(row["_model"])
    campaign_id = str(row["campaign_id"])
    property_code = str(row["property_code"])
    recipient = str(row["owner_email"]).strip().casefold()
    base_url = (Config.CRM_BASE_URL or "https://www.procasa.cl").rstrip("/")
    expires_at = int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp())

    def response_url(action: str, token_action: str, *, document_type: str | None = None) -> str:
        token = issue_live_token(
            campaign_id=campaign_id,
            property_code=property_code,
            action=token_action,
            recipient=recipient,
            document_type=document_type,
            expires_at=expires_at,
        )
        if token_action == "ver_informe":
            return base_url + "/campana/informe?" + urlencode({"token": token})
        return base_url + "/campana/respuesta?" + urlencode({
            "email": recipient,
            "campana": campaign_id,
            "codigos": property_code,
            "mode": "owner_campaign",
            "accion": action,
            "token": token,
        })

    model["cta"] = {
        **dict(model.get("cta") or {}),
        "primary_url": response_url("aceptar_rebaja", "aceptar_rebaja"),
        "primary_label": "REVISAR / CONFIRMAR AJUSTE",
        "advisor_url": response_url("contactar_ejecutivo", "contactar_ejecutivo"),
    }
    model["document"] = dict(model.get("document") or {})
    if model["document"].get("visible"):
        document_type = str(row.get("document_type") or "").upper()
        if document_type in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
            model["cta"]["report_url"] = response_url(
                "ver_informe", "ver_informe", document_type=document_type,
            )
        else:
            model["cta"]["report_url"] = ""
    else:
        model["cta"]["report_url"] = ""
    return render_owner_campaign_email_v2(
        [model], email=row["owner_email"], executives=[model["executive"]],
        base_url=base_url,
    )


if __name__ == "__main__":
    result = prepare(write_ledger=True)
    batch = result.pop("batch")
    safe = {key: value for key, value in result.items()}
    print(json.dumps(safe, ensure_ascii=False, default=str, indent=2))
    print("BATCH_SANITY=" + json.dumps({
        "candidates": len(batch),
        "all_to_valid": all(runtime._valid_owner_email(row["owner_email"]) for row in batch),
        "all_boss_cc": all(runtime._valid_owner_email(row["boss_cc"]) for row in batch),
        "all_executive_cc": all(runtime._valid_owner_email(row["executive_email"]) for row in batch),
        "all_adjustment": all(5 <= row["recommended_adjustment_pct"] <= 10 for row in batch),
        "all_recommendation_sections": all("Ajuste de precio sugerido" in row["rendered_html"] and "Recomendación PROCASA" in row["rendered_html"] for row in batch),
        "all_accept_cta": all("ACEPTAR NUEVO VALOR" in row["rendered_html"] for row in batch),
        "all_advisor_cta": all("REVISAR CON MI EJECUTIVO" in row["rendered_html"] for row in batch),
        "unique_owners": len({row["owner_email"] for row in batch}) == len(batch),
        "unique_properties": len({row["property_code"] for row in batch}) == len(batch),
        "owner_emails_sent": 0,
    }, ensure_ascii=False))
