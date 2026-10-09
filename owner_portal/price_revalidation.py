"""Source-currency-aware validation for owner campaign recommendations.

Campaign prices are displayed in UF, but some properties are published in CLP.
For those properties, the published CLP amount is the canonical value; a daily
UF conversion must not be mistaken for a change to that published price.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping


VALIDATOR_VERSION = "owner-price-source-v1"
DEFAULT_UF_SOURCE_URL = "https://www.sii.cl/valores_y_fechas/uf/uf2026.htm"


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", ".")).quantize(Decimal("0.00000001"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _money_round(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _source_fields(property_doc: Mapping[str, Any], operation: str) -> dict[str, Any] | None:
    operation_data = property_doc.get("tipo_operacion")
    operation_data = operation_data if isinstance(operation_data, Mapping) else {}
    block_name = "precio_venta" if operation == "VENTA" else "precio_arriendo"
    block = operation_data.get(block_name)
    block = block if isinstance(block, Mapping) else {}
    published_currency = str(block.get("moneda_publicada") or "").strip().upper()
    publication_currency = str(block.get("moneda_publicacion") or "").strip().upper()
    if published_currency and publication_currency and published_currency != publication_currency:
        return None
    currency = published_currency or publication_currency
    if currency not in {"UF", "CLP"}:
        return None
    raw_amount = block.get("precio_publicado")
    source_field = f"tipo_operacion.{block_name}.precio_publicado"
    if currency == "UF":
        raw_amount = block.get("precio_publicado_original")
        if raw_amount in (None, ""):
            raw_amount = block.get("precio_publicado")
        if raw_amount in (None, ""):
            raw_amount = block.get("precio_uf")
            source_field = f"tipo_operacion.{block_name}.precio_uf"
        uf_value = _decimal(block.get("precio_uf"))
        amount = _decimal(raw_amount)
        alternate_amounts = (
            _decimal(block.get("precio_publicado")),
            _decimal(block.get("precio_publicado_original")),
        )
        if (
            amount is None or amount <= 0 or uf_value is None or abs(amount - uf_value) > Decimal("0.01")
            or any(candidate is not None and abs(candidate - amount) > Decimal("0.01") for candidate in alternate_amounts)
        ):
            return None
    else:
        if raw_amount in (None, ""):
            raw_amount = block.get("precio_clp")
            source_field = f"tipo_operacion.{block_name}.precio_clp"
        amount = _decimal(raw_amount)
        clp_value = _decimal(block.get("precio_clp"))
        original_amount = _decimal(block.get("precio_publicado_original"))
        if (
            amount is None or amount <= 0 or clp_value is None or abs(amount - clp_value) > Decimal("0.5")
            or (original_amount is not None and abs(original_amount - amount) > Decimal("0.5"))
        ):
            return None
        uf_value = None
    return {
        "block": block,
        "currency": currency,
        "amount": amount,
        "field": source_field,
        "uf_value": uf_value,
    }


def build_price_revalidation(
    row: Mapping[str, Any], property_doc: Mapping[str, Any], *,
    validated_at: datetime | str, uf_rate_clp: Any, uf_rate_date: str,
    uf_source_url: str = DEFAULT_UF_SOURCE_URL,
) -> dict[str, Any]:
    """Build a complete, auditable price recommendation from the live source."""
    from campanas import owner_campaign_test_runtime as runtime

    snapshot = row.get("campaign_snapshot") if isinstance(row.get("campaign_snapshot"), Mapping) else {}
    code = str(row.get("property_code") or "").strip()
    operation = str(
        snapshot.get("operation_resolved") or snapshot.get("operation")
        or row.get("operation_resolved") or row.get("operation") or ""
    ).strip().upper()
    if not code or str(property_doc.get("codigo") or "").strip() != code:
        raise ValueError("PROPERTY_IDENTITY_NOT_VERIFIABLE")
    if operation not in {"VENTA", "ARRIENDO"}:
        raise ValueError("OPERATION_NOT_VERIFIABLE")
    if runtime.resolve_property_operation(property_doc) not in {operation, runtime.VENTA_ARRIENDO}:
        raise ValueError("PROPERTY_OPERATION_MISMATCH")
    state = property_doc.get("estado") if isinstance(property_doc.get("estado"), Mapping) else {}
    if str(state.get("estado_prop360") or "").strip().casefold() != "activa" or state.get("disponible_prop360") is not True:
        raise ValueError("PROPERTY_NOT_ACTIVE_OR_AVAILABLE")

    source = _source_fields(property_doc, operation)
    if source is None:
        raise ValueError("CANONICAL_PUBLISHED_PRICE_NOT_VERIFIABLE")
    rate = _decimal(uf_rate_clp)
    if rate is None or rate <= 0 or not uf_rate_date or not uf_source_url.startswith("https://www.sii.cl/"):
        raise ValueError("UF_CONVERSION_NOT_VERIFIABLE")
    pct = _decimal(snapshot.get("recommended_adjustment_pct", row.get("recommended_adjustment_pct")))
    if pct is None or pct != pct.to_integral_value() or pct < 5 or pct > 10:
        raise ValueError("RECOMMENDATION_PERCENT_OUT_OF_POLICY")

    current_uf = source["amount"] if source["currency"] == "UF" else source["amount"] / rate
    recommended_uf = (current_uf * (Decimal("100") - pct) / Decimal("100")).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP,
    )
    current_clp = _money_round(current_uf * rate) if source["currency"] == "UF" else _money_round(source["amount"])
    recommended_clp = (
        _money_round(source["amount"] * (Decimal("100") - pct) / Decimal("100"))
        if source["currency"] == "CLP"
        else _money_round(recommended_uf * rate)
    )
    original_current = _decimal(snapshot.get("current_price", row.get("current_price")))
    original_recommended = _decimal(snapshot.get("recommended_price", row.get("recommended_price")))
    original_pct = _decimal(snapshot.get("recommended_adjustment_pct", row.get("recommended_adjustment_pct")))
    stamp = validated_at.isoformat() if isinstance(validated_at, datetime) else str(validated_at)
    return {
        "status": "VALIDATED",
        "validator_version": VALIDATOR_VERSION,
        "validated_at": stamp,
        "campaign_id": str(row.get("campaign_id") or ""),
        "property_code": code,
        "operation": operation,
        "source_currency": source["currency"],
        "source_amount": float(source["amount"]),
        "source_field": source["field"],
        "source_collection": "universo_cartera_prop360",
        "conversion": {
            "uf_rate_clp": float(rate),
            "uf_rate_date": str(uf_rate_date),
            "source": "SII",
            "source_url": uf_source_url,
        },
        "current_price_uf": float(current_uf),
        "recommended_adjustment_pct": int(pct),
        "recommended_price_uf": float(recommended_uf),
        "current_price_clp": current_clp,
        "recommended_price_clp": recommended_clp,
        "calculation": "current published price × (100 - adjustment percentage) / 100",
        "rounding": {"UF_decimals": 6, "CLP": "integer, half up"},
        "previous_campaign": {
            "current_price_uf": float(original_current) if original_current is not None else None,
            "recommended_adjustment_pct": int(original_pct) if original_pct is not None else None,
            "recommended_price_uf": float(original_recommended) if original_recommended is not None else None,
        },
    }


def validate_price_revalidation(
    row: Mapping[str, Any], property_doc: Mapping[str, Any], *, operation: str,
) -> dict[str, Any]:
    """Validate a saved recommendation against the live published source."""
    code = str(row.get("property_code") or "").strip()
    value = row.get("price_revalidation")
    if not isinstance(value, Mapping) or str(value.get("status") or "").upper() != "VALIDATED":
        return {"required": True, "reason": "PRICE_REVALIDATION_NOT_VALIDATED"}
    if (
        str(property_doc.get("codigo") or "").strip() != code
        or str(value.get("property_code") or "").strip() != code
        or str(value.get("campaign_id") or "") != str(row.get("campaign_id") or "")
        or str(value.get("operation") or "").upper() != operation
    ):
        return {"required": True, "reason": "PRICE_REVALIDATION_IDENTITY_MISMATCH"}
    from campanas import owner_campaign_test_runtime as runtime
    resolved_operation = runtime.resolve_property_operation(property_doc)
    if resolved_operation in {"VENTA", "ARRIENDO"} and resolved_operation != operation:
        return {"required": True, "reason": "PROPERTY_OPERATION_MISMATCH"}
    state = property_doc.get("estado") if isinstance(property_doc.get("estado"), Mapping) else {}
    if str(state.get("estado_prop360") or "").strip().casefold() != "activa" or state.get("disponible_prop360") is not True:
        return {"required": True, "reason": "PROPERTY_NOT_ACTIVE_OR_AVAILABLE"}
    source = _source_fields(property_doc, operation)
    if source is None:
        return {"required": True, "reason": "CANONICAL_PUBLISHED_PRICE_NOT_VERIFIABLE"}
    saved_currency = str(value.get("source_currency") or "").upper()
    saved_amount = _decimal(value.get("source_amount"))
    if saved_currency != source["currency"] or saved_amount is None or abs(saved_amount - source["amount"]) > Decimal("0.5"):
        return {"required": True, "reason": "LIVE_PUBLISHED_PRICE_CHANGED"}
    conversion = value.get("conversion") if isinstance(value.get("conversion"), Mapping) else {}
    rate = _decimal(conversion.get("uf_rate_clp"))
    if (
        rate is None or rate <= 0 or not str(conversion.get("uf_rate_date") or "")
        or str(conversion.get("source") or "").upper() != "SII"
        or not str(conversion.get("source_url") or "").startswith("https://www.sii.cl/")
    ):
        return {"required": True, "reason": "UF_CONVERSION_NOT_VERIFIABLE"}
    current = _decimal(value.get("current_price_uf"))
    pct = _decimal(value.get("recommended_adjustment_pct"))
    recommended = _decimal(value.get("recommended_price_uf"))
    snapshot = row.get("campaign_snapshot") if isinstance(row.get("campaign_snapshot"), Mapping) else {}
    campaign_pct = _decimal(snapshot.get("recommended_adjustment_pct", row.get("recommended_adjustment_pct")))
    if (
        current is None or pct is None or pct != pct.to_integral_value() or pct < 5 or pct > 10
        or campaign_pct is None or pct != campaign_pct
        or recommended is None
    ):
        return {"required": True, "reason": "RECOMMENDATION_NOT_VERIFIABLE"}
    expected_current = source["amount"] if source["currency"] == "UF" else source["amount"] / rate
    expected_recommended = (expected_current * (Decimal("100") - pct) / Decimal("100")).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP,
    )
    if abs(current - expected_current) > Decimal("0.000001"):
        return {"required": True, "reason": "PRICE_REVALIDATION_BASE_MISMATCH"}
    if abs(recommended - expected_recommended) > Decimal("0.000001"):
        return {"required": True, "reason": "RECOMMENDATION_ARITHMETIC_MISMATCH"}
    expected_current_clp = _money_round(expected_current * rate) if source["currency"] == "UF" else _money_round(source["amount"])
    expected_recommended_clp = (
        _money_round(source["amount"] * (Decimal("100") - pct) / Decimal("100"))
        if source["currency"] == "CLP"
        else _money_round(expected_recommended * rate)
    )
    if _decimal(value.get("current_price_clp")) != Decimal(expected_current_clp):
        return {"required": True, "reason": "PRICE_REVALIDATION_CLP_MISMATCH"}
    if _decimal(value.get("recommended_price_clp")) != Decimal(expected_recommended_clp):
        return {"required": True, "reason": "RECOMMENDATION_CLP_MISMATCH"}
    return {
        "required": False,
        "reason": "",
        "operation": operation,
        "source_currency": source["currency"],
        "source_amount": float(source["amount"]),
        "live_current_price": float(expected_current),
        "recommended_adjustment_pct": int(pct),
        "recommended_price": float(expected_recommended),
        "current_price_clp": expected_current_clp,
        "recommended_price_clp": expected_recommended_clp,
        "price_revalidation": dict(value),
    }
