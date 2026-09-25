"""Generate the small, PII-free QA fixture from approved campaign reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


EXPECTED_SEGMENT_COUNTS = {
    "STRONG_PRICE_ADJUSTMENT": 45,
    "MIXED_EVIDENCE": 180,
    "COMPETITIVE_LOW_RESPONSE": 9,
    "INSUFFICIENT_EVIDENCE": 172,
}
FIXED_CASE_CODES = {"A": "5641", "B": "16521", "D": "16527"}
SII_BUILT_SOURCE = "tasaciones.tasacion_online.total_construccion_m2:SII"


class FixtureGenerationError(ValueError):
    """An approved report is missing or contradicts the fixture contract."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise FixtureGenerationError(f"invalid_{field}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise FixtureGenerationError(f"missing_{field}") from exc
    if not math.isfinite(result) or result <= 0:
        raise FixtureGenerationError(f"invalid_{field}")
    return result


def _records_by_code(report: Mapping[str, Any], field: str) -> dict[str, dict[str, Any]]:
    rows = report.get(field)
    if not isinstance(rows, list):
        raise FixtureGenerationError(f"{field}_missing")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise FixtureGenerationError(f"{field}_row_invalid")
        code = str(row.get("codigo") or "").strip()
        if not code or code in indexed:
            raise FixtureGenerationError(f"{field}_code_invalid_or_duplicate")
        indexed[code] = dict(row)
    return indexed


def _document_type(row: Mapping[str, Any]) -> str:
    if row.get("appraisal_document_available") and row.get("appraisal_renderable"):
        return "INDIVIDUAL_APPRAISAL"
    if row.get("communal_document_available") and row.get("communal_renderable"):
        return "COMMUNAL_MARKET_REPORT"
    return "NONE"


def _appraisal_values(row: Mapping[str, Any], code: str) -> dict[str, float | None]:
    appraisal = row.get("appraisal")
    if not isinstance(appraisal, Mapping):
        raise FixtureGenerationError(f"appraisal_missing_{code}")
    return {
        "low_uf": _positive_number(appraisal.get("low"), f"appraisal_low_{code}") if appraisal.get("low") is not None else None,
        "mid_uf": _positive_number(appraisal.get("mid"), f"appraisal_mid_{code}") if appraisal.get("mid") is not None else None,
        "high_uf": _positive_number(appraisal.get("high"), f"appraisal_high_{code}") if appraisal.get("high") is not None else None,
    }


def _sale_display_target(raw_target: float) -> float:
    """Apply the already-approved sales display rule: nearest whole UF."""
    return float(math.floor(raw_target + 0.5))


def build_fixture(
    structural_bytes: bytes,
    price_policy_bytes: bytes,
    *,
    structural_filename: str,
    price_policy_filename: str,
) -> dict[str, Any]:
    try:
        structural = json.loads(structural_bytes)
        price_policy = json.loads(price_policy_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureGenerationError("approved_report_json_invalid") from exc
    if not isinstance(structural, Mapping) or not isinstance(price_policy, Mapping):
        raise FixtureGenerationError("approved_report_root_invalid")

    records = _records_by_code(structural, "records")
    segments_by_code = {
        code: str(row.get("evidence_segment") or "").strip().upper()
        for code, row in records.items()
    }
    if len(records) != 406 or any(not value for value in segments_by_code.values()):
        raise FixtureGenerationError("approved_structural_records_invalid")
    segment_counts = dict(sorted(Counter(segments_by_code.values()).items()))
    if segment_counts != dict(sorted(EXPECTED_SEGMENT_COUNTS.items())):
        raise FixtureGenerationError("approved_segment_counts_mismatch")
    declared_counts = structural.get("evidence_segments")
    if not isinstance(declared_counts, Mapping) or dict(declared_counts) != EXPECTED_SEGMENT_COUNTS:
        raise FixtureGenerationError("declared_segment_counts_mismatch")

    policy_records = _records_by_code(price_policy, "properties")
    cases: dict[str, dict[str, Any]] = {}
    for case_id, code in FIXED_CASE_CODES.items():
        row = records.get(code)
        if row is None:
            raise FixtureGenerationError(f"fixed_case_missing_{case_id}")
        document_type = _document_type(row)
        if document_type == "NONE":
            raise FixtureGenerationError(f"fixed_case_document_missing_{case_id}")
        case: dict[str, Any] = {
            "property_code": code,
            "evidence_segment": segments_by_code[code],
            "document_type": document_type,
            "source_lineage": {
                "evidence_segment": f"{structural_filename}:records[codigo={code}].evidence_segment",
                "document_type": f"{structural_filename}: appraisal/communal availability flags; individual appraisal precedence",
            },
        }
        if case_id in {"A", "B"}:
            case["appraisal"] = _appraisal_values(row, code)
            case["source_lineage"]["appraisal"] = (
                "analytics.owner_campaign_operation_evidence_dry_run.appraisal_values <- "
                "tasaciones.tasacion_online"
            )
        if case_id == "A":
            target = policy_records.get(code)
            if (
                target is None
                or target.get("decision") != "PRICE_AUTHORIZATION_READY"
                or target.get("price_authorization_ready") is not True
                or target.get("operation") != "VENTA"
            ):
                raise FixtureGenerationError("case_a_approved_target_missing")
            raw_target = _positive_number(target.get("raw_target"), "case_a_raw_target")
            case["raw_recommended_price_uf"] = raw_target
            case["display_recommended_price_uf"] = _sale_display_target(raw_target)
            case["source_lineage"]["raw_recommended_price"] = (
                f"{price_policy_filename}:properties[codigo={code}].raw_target"
            )
            case["source_lineage"]["display_recommended_price"] = (
                "approved display rule: VENTA rounded to nearest whole UF, applied to raw_target"
            )
        elif case_id == "B":
            golden = structural.get("golden_16521")
            if not isinstance(golden, Mapping):
                raise FixtureGenerationError("case_b_golden_missing")
            built_source = str(golden.get("built_area_source") or "")
            if built_source != SII_BUILT_SOURCE:
                raise FixtureGenerationError("case_b_sii_source_mismatch")
            case["total_construccion_m2"] = _positive_number(golden.get("built_m2"), "case_b_built_m2")
            case["built_m2_source"] = built_source
            case["source_lineage"]["built_m2"] = built_source
        else:
            appraisal = row.get("appraisal")
            if not isinstance(appraisal, Mapping):
                raise FixtureGenerationError("case_d_appraisal_missing")
            case["rent_estimate_uf"] = _positive_number(appraisal.get("rent"), "case_d_rent_estimate")
            case["source_lineage"]["rent_estimate"] = (
                "analytics.owner_campaign_operation_evidence_dry_run.appraisal_values <- "
                "tasaciones.tasacion_online.arriendo_estimado.uf"
            )
        cases[case_id] = case

    return {
        "schema_version": "owner_campaign_qa_evidence_v1",
        "source_report_filename": structural_filename,
        "source_report_sha256": _sha256(structural_bytes),
        "price_policy_report_filename": price_policy_filename,
        "price_policy_report_sha256": _sha256(price_policy_bytes),
        # Preserve the approved snapshot timestamp so regenerating is byte-stable.
        "generated_at": str(structural.get("generated_at") or ""),
        "record_count": len(records),
        "segment_counts": segment_counts,
        "cases": cases,
        "segments_by_code": dict(sorted(segments_by_code.items(), key=lambda item: item[0])),
    }


def generate_fixture(source_report: Path, price_policy_report: Path, output: Path) -> dict[str, Any]:
    structural_bytes = source_report.read_bytes()
    price_policy_bytes = price_policy_report.read_bytes()
    fixture = build_fixture(
        structural_bytes,
        price_policy_bytes,
        structural_filename=source_report.name,
        price_policy_filename=price_policy_report.name,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(fixture, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return fixture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--price-policy-report", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "fixtures" / "owner_campaign_qa_evidence_20260923.json",
    )
    args = parser.parse_args()
    try:
        fixture = generate_fixture(args.source_report, args.price_policy_report, args.output)
    except (OSError, FixtureGenerationError) as exc:
        parser.error(str(exc))
    print(f"QA_FIXTURE={args.output}")
    print(f"QA_EVIDENCE_SOURCE={fixture['source_report_filename']}")
    print(f"QA_EVIDENCE_SHA256={fixture['source_report_sha256']}")
    print(f"QA_EVIDENCE_RECORDS={fixture['record_count']}")
    print(f"QA_SEGMENT_COUNTS={json.dumps(fixture['segment_counts'], sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
