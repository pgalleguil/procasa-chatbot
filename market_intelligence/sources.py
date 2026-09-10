from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import MarketIntelligenceSnapshotV1

BDE_API_ENDPOINT = "https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx"


@dataclass(frozen=True)
class OfficialIndicatorSourceV1:
    indicator_id: str
    source_name: str
    geography: str
    series_id: str
    unit: str
    frequency: str
    source_reference: str
    value_transform: str = "identity"


OFFICIAL_SOURCES: tuple[OfficialIndicatorSourceV1, ...] = (
    OfficialIndicatorSourceV1(
        indicator_id="mortgage_rate_uf",
        source_name="Banco Central de Chile",
        geography="Chile",
        series_id="F022.VIV.TIP.MA03.UF.Z.M",
        unit="porcentaje anualizado",
        frequency="mensual",
        source_reference=(
            "https://si3.bcentral.cl/siete/ES/Siete/Cuadro/CAP_TASA_INTERES/"
            "MN_TASA_INTERES_09/TSF_27?idSerie=F022.VIV.TIP.MA03.UF.Z.M"
        ),
    ),
    OfficialIndicatorSourceV1(
        indicator_id="housing_price_index",
        source_name="Banco Central de Chile",
        geography="Chile",
        series_id="F034.IPV.FLU.BCCH.2008.0.T",
        unit="índice base 2008",
        frequency="trimestral",
        source_reference=(
            "https://si3.bcentral.cl/siete/ES/Siete/Cuadro/CAP_ESTADIST_EXPERIM/"
            "MN_EXPERIM01/IS_GENERAL_PROPIEDAD_08?idSerie=F034.IPV.FLU.BCCH.2008.0.T"
        ),
    ),
    OfficialIndicatorSourceV1(
        indicator_id="ipc_monthly_change",
        source_name="Instituto Nacional de Estadísticas",
        geography="Chile",
        series_id="F074.IPC.VAR.Z.2023.C.M",
        unit="variación mensual porcentual",
        frequency="mensual",
        source_reference=(
            "https://si3.bcentral.cl/siete/ES/Siete/Cuadro/CAP_PRECIOS/"
            "MN_CAP_PRECIOS/IPC_G_2023?idSerie=F074.IPC.VAR.Z.2023.C.M"
        ),
    ),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).strip().replace("\u00a0", "").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        return float(text)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _request_source(source: OfficialIndicatorSourceV1, *, token: str | None, timeout: int) -> MarketIntelligenceSnapshotV1:
    retrieved_at = _utc_now().isoformat()
    endpoint = BDE_API_ENDPOINT
    provenance = {
        "endpoint": endpoint,
        "series_id": source.series_id,
        "frequency": source.frequency,
        "parsing_status": "not_attempted",
        "write_mode": "dry_run_only",
    }
    if not token:
        provenance["parsing_status"] = "blocked_missing_api_token"
        return MarketIntelligenceSnapshotV1(
            indicator_id=source.indicator_id,
            scope="nacional",
            geography=source.geography,
            value=None,
            unit=source.unit,
            reference_period="",
            source_name=source.source_name,
            source_reference=source.source_reference,
            retrieved_at_utc=retrieved_at,
            source_published_at=None,
            status="not_configured",
            provenance=provenance,
        )

    today = _utc_now().date()
    query = urllib.parse.urlencode(
        {
            "token": token,
            "function": "GetSeries",
            "timeseries": source.series_id,
            "firstdate": (today - timedelta(days=400)).isoformat(),
            "lastdate": today.isoformat(),
        }
    )
    request = urllib.request.Request(
        f"{endpoint}?{query}",
        headers={"User-Agent": "procasa-market-intelligence-dry-run/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        provenance["parsing_status"] = "request_failed"
        provenance["error_type"] = type(exc).__name__
        return MarketIntelligenceSnapshotV1(
            indicator_id=source.indicator_id,
            scope="nacional",
            geography=source.geography,
            value=None,
            unit=source.unit,
            reference_period="",
            source_name=source.source_name,
            source_reference=source.source_reference,
            retrieved_at_utc=retrieved_at,
            source_published_at=None,
            status="error",
            provenance=provenance,
        )

    series = payload.get("Series") if isinstance(payload, dict) else None
    observations = series.get("Obs") if isinstance(series, dict) else None
    valid = []
    for observation in observations or []:
        if not isinstance(observation, dict) or observation.get("statusCode") not in {None, "OK"}:
            continue
        value = _parse_value(observation.get("value"))
        period = str(observation.get("indexDateString") or "").strip()
        if value is not None and period:
            valid.append((period, value))
    if not valid:
        provenance["parsing_status"] = "no_valid_observation"
        return MarketIntelligenceSnapshotV1(
            indicator_id=source.indicator_id,
            scope="nacional",
            geography=source.geography,
            value=None,
            unit=source.unit,
            reference_period="",
            source_name=source.source_name,
            source_reference=source.source_reference,
            retrieved_at_utc=retrieved_at,
            source_published_at=None,
            status="no_data",
            provenance=provenance,
        )
    period, value = valid[-1]
    provenance["parsing_status"] = "ok"
    provenance["observations_read"] = len(valid)
    return MarketIntelligenceSnapshotV1(
        indicator_id=source.indicator_id,
        scope="nacional",
        geography=source.geography,
        value=round(value, 6),
        unit=source.unit,
        reference_period=period,
        source_name=source.source_name,
        source_reference=source.source_reference,
        retrieved_at_utc=retrieved_at,
        source_published_at=None,
        status="valid",
        provenance=provenance,
    )


def run_dry_run(*, token: str | None = None, timeout: int = 8) -> dict[str, Any]:
    """Inspect official sources without Mongo writes or portal pageview calls."""

    resolved_token = token if token is not None else os.getenv("BCCH_BDE_API_TOKEN")
    records = [_request_source(source, token=resolved_token, timeout=timeout) for source in OFFICIAL_SOURCES]
    return {
        "mode": "dry-run",
        "writes": 0,
        "mongo_writes": 0,
        "pageview_fetches": 0,
        "records": [record.to_dict() for record in records],
    }
