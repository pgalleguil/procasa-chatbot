"""Read-only portfolio audit for Owner Portal Engine V1.

The script reads the configured property, publication, lead, cohort and
market collections through the existing foundation service.  It never calls
an insert, update, delete, replacement, or persistence API and emits only
analytical aggregates plus the three requested control cases.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _parse_as_of(value: str | None) -> datetime:
    from analytics.pricing_intelligence.time_utils import BUSINESS_TZ

    if not value:
        return datetime.now(BUSINESS_TZ)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--as-of must be timezone-aware")
    return parsed


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _engine_input(view: Any) -> dict[str, Any]:
    cohort = view.comparable_cohort
    return {
        "operation": view.operation,
        "current_price_uf": view.current_price_uf,
        "cohort": (
            {
                "n": cohort.count,
                "p25": cohort.p25_uf,
                "p40": None,
                "p50": cohort.median_uf,
                "p60": cohort.p60_uf,
                "p75": cohort.p75_uf,
                "p90": cohort.p90_uf,
                "ecdf": cohort.market_ecdf,
                "deterministic": bool(cohort.cohort_fingerprint),
                "fingerprint": cohort.cohort_fingerprint,
                "robustness_warning": False,
            }
            if cohort
            else None
        ),
        "commercial_response": {
            "inquiries_30d": view.inquiries_previous_30d,
            "inquiries_90d": view.inquiries_previous_90d,
            "signal_30d": view.demand_signal_30d,
            "signal_90d": view.demand_signal_90d,
            "confidence_30d": view.demand_confidence_30d,
            "confidence_90d": view.demand_confidence_90d,
        },
        "exposure": {
            "state": "LOW" if not view.publications else "PARTIAL" if len(view.publications) == 1 else "ADEQUATE",
            "active_portals": tuple(publication.portal_id for publication in view.publications),
        },
        "observation": {},
        "context": {
            "page_as_of": view.page_as_of,
            "last_price_change_at": view.last_price_change_at,
            "operation_ambiguity": view.operation_selection_required,
            "geography_valid": bool(view.canonical_region and view.commune),
            "critical_conflict": False,
        },
    }


def _control_summary(code: str, view: Any) -> dict[str, Any] | None:
    if view is None:
        return None
    decision = view.engine_v1
    cohort = view.comparable_cohort
    return {
        "property_code": str(code),
        "operation": view.operation,
        "current_price_uf": view.current_price_uf,
        "cohort_n": cohort.count if cohort else 0,
        "p75": cohort.p75_uf if cohort else None,
        "p90": cohort.p90_uf if cohort else None,
        "market_position": decision.market_position,
        "gap_to_p75_pct": decision.gap_to_p75_pct,
        "status": decision.status,
        "recommendation": decision.recommendation,
        "eligibility": decision.eligibility,
        "confidence": decision.confidence,
        "gradual_price_uf": decision.gradual_price_uf,
        "competitive_reference_uf": decision.competitive_reference_uf,
        "owner_action": decision.owner_action,
        "inquiries_30d": view.inquiries_previous_30d,
        "inquiries_90d": view.inquiries_previous_90d,
        "active_portal_count": len(view.publications),
        "reasons": list(decision.reasons),
        "warnings": list(decision.warnings),
    }


def _invariant_report(records: list[tuple[str, Any]]) -> dict[str, bool]:
    from analytics.pricing_intelligence.engine_v1 import evaluate_engine_v1

    decisions = [(code, view, view.engine_v1) for code, view in records]
    zero_uncertain_safe = True
    insufficient_cohort_safe = True
    rent_safe = True
    trivial_gap_safe = True
    rounding_safe = True
    cooldown_safe = True
    red_action_safe = True
    gap8_action_safe = True
    robustness_action_safe = True
    deterministic = True

    for _code, view, decision in decisions:
        cohort = view.comparable_cohort
        n = cohort.count if cohort else 0
        gap = decision.gap_to_p75_pct
        target = decision.gradual_price_uf
        if n < 8 and target is not None:
            insufficient_cohort_safe = False
        if str(view.operation or "").casefold() == "arriendo" and target is not None:
            rent_safe = False
        if gap is None or gap < 2:
            if target is not None:
                trivial_gap_safe = False
        if target is not None:
            p75 = cohort.p75_uf if cohort else None
            current = _number(view.current_price_uf)
            mathematical = (current + p75) / 2 if current is not None and p75 is not None else None
            if current is None or mathematical is None or target > current or target > mathematical:
                rounding_safe = False
        if "PRICE_CHANGE_COOLDOWN_ACTIVE" in decision.warnings:
            if target is not None or decision.recommendation == "MARKET_REPOSITIONING":
                cooldown_safe = False
        if decision.eligibility == "RED" and decision.owner_action == "CAN_REQUEST_PRICE_CHANGE":
            red_action_safe = False
        if gap is not None and gap > 8 and decision.owner_action == "CAN_REQUEST_PRICE_CHANGE":
            gap8_action_safe = False
        if "COHORT_ROBUSTNESS_WARNING" in decision.warnings and decision.owner_action == "CAN_REQUEST_PRICE_CHANGE":
            robustness_action_safe = False
        if view.demand_signal_30d == "ZERO_UNCERTAIN" and target is not None:
            # A target is valid only when independent market evidence is present.
            zero_uncertain_safe = zero_uncertain_safe and n >= 8 and gap is not None and gap >= 2 and decision.recommendation == "MARKET_REPOSITIONING"
        again = evaluate_engine_v1(**_engine_input(view))
        deterministic = deterministic and again.to_dict() == decision.to_dict()
    return {
        "zero_uncertain_safe": zero_uncertain_safe,
        "insufficient_cohort_safe": insufficient_cohort_safe,
        "rent_safe": rent_safe,
        "trivial_gap_safe": trivial_gap_safe,
        "rounding_safe": rounding_safe,
        "cooldown_safe": cooldown_safe,
        "red_action_safe": red_action_safe,
        "gap8_action_safe": gap8_action_safe,
        "robustness_action_safe": robustness_action_safe,
        "deterministic": deterministic,
    }


def run_audit(
    *,
    db: Any,
    as_of: datetime,
    control_codes: tuple[str, ...] = ("6786", "6464", "6414"),
    workers: int = 8,
) -> dict[str, Any]:
    from owner_portal.service import (
        MASTER_PUBLICATION_PROJECTION,
        get_owner_portal_property_view,
    )
    from owner_portal.semantics import resolve_property_operations

    documents = list(
        db["universo_cartera_prop360"].find(
            {"estado.oficina": "PROCASA SUCRE", "estado.disponible_prop360": True},
            MASTER_PUBLICATION_PROJECTION,
        )
    )
    sale_codes = [
        str(document.get("codigo"))
        for document in documents
        if document.get("codigo") is not None and "venta" in resolve_property_operations(document).get("operations", ())
    ]
    records: list[tuple[str, Any]] = []
    missing: list[str] = []
    unique_sale_codes = sorted(set(sale_codes))
    max_workers = max(1, min(int(workers), 32))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(get_owner_portal_property_view, db, code, as_of): code
            for code in unique_sale_codes
        }
        for index, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            view = future.result()
            if view is None:
                missing.append(code)
            else:
                records.append((code, view))
            if index % 50 == 0:
                print(f"AUDIT_PROGRESS={index}/{len(unique_sale_codes)}", flush=True)

    recommendations = Counter(view.engine_v1.recommendation for _code, view in records)
    eligibilities = Counter(view.engine_v1.eligibility for _code, view in records)
    confidences = Counter(view.engine_v1.confidence for _code, view in records)
    actions = Counter(view.engine_v1.owner_action for _code, view in records)
    numeric_count = sum(view.engine_v1.gradual_price_uf is not None for _code, view in records)
    requestable_count = actions["CAN_REQUEST_PRICE_CHANGE"]
    invariants = _invariant_report(records)
    controls = {}
    for code in control_codes:
        view = next((view for item_code, view in records if item_code == code), None)
        controls[code] = _control_summary(code, view)

    expected_numeric = 46
    expected_requestable = 5
    numeric_delta = numeric_count - expected_numeric
    requestable_delta = requestable_count - expected_requestable
    return {
        "as_of": as_of.isoformat(),
        "total_sale": len(records),
        "missing_views": len(missing),
        "recommendations": dict(sorted(recommendations.items())),
        "eligibility": dict(sorted(eligibilities.items())),
        "confidence": dict(sorted(confidences.items())),
        "owner_action": dict(sorted(actions.items())),
        "numeric_recommendations": {"count": numeric_count, "pct": round(numeric_count / len(records) * 100, 2) if records else 0.0},
        "requestable": {"count": requestable_count, "pct": round(requestable_count / len(records) * 100, 2) if records else 0.0},
        "controls": controls,
        "invariants": invariants,
        "portfolio_drift_vs_2d3": {
            "expected_numeric_approximately": expected_numeric,
            "actual_numeric": numeric_count,
            "numeric_delta": numeric_delta,
            "expected_requestable_approximately": expected_requestable,
            "actual_requestable": requestable_count,
            "requestable_delta": requestable_delta,
            "material": abs(numeric_delta) > 5 or abs(requestable_delta) > round(len(records) * 0.02),
        },
        "mongo_writes": 0,
        "audit_pass": not missing and all(invariants.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Owner Portal Engine V1 audit")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file, override=True)
    else:
        load_dotenv(override=False)
    uri = os.getenv("MONGO_URI")
    if not uri:
        raise SystemExit("MONGO_URI is required")
    db_name = os.getenv("DB_NAME", "URLS")
    as_of = _parse_as_of(args.as_of)
    client = MongoClient(uri, serverSelectionTimeoutMS=30000)
    try:
        client.admin.command("ping")
        result = run_audit(db=client[db_name], as_of=as_of, workers=args.workers)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["audit_pass"] else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
