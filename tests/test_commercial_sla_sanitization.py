import json
import math
from pathlib import Path

from analytics.leads_queries import _sla_percentile
from analytics.leads_service import _sanitize_non_finite


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "templates" / "analytics" / "commercial_dashboard.html"


def test_sla_percentiles_discard_non_finite_values_and_return_null_when_empty():
    assert _sla_percentile([math.nan, math.inf, -math.inf], 50) is None
    assert _sla_percentile([math.nan, 30, 60], 50) == 45.0


def test_commercial_payload_serializes_without_nan_or_infinity():
    payload = _sanitize_non_finite({
        "lead": {"median_minutes": math.nan, "p90_minutes": math.inf},
        "lead_hot": {"median_minutes": -math.inf, "p90_minutes": 30.0},
        "nested": [math.nan, {"value": math.inf}],
    })
    encoded = json.dumps(payload, allow_nan=False)
    assert "NaN" not in encoded
    assert "Infinity" not in encoded
    assert payload["lead"]["median_minutes"] is None
    assert payload["lead"]["p90_minutes"] is None
    assert payload["lead_hot"]["median_minutes"] is None


def test_sla_template_renders_non_finite_temporal_values_as_si():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "Number.isFinite" in html
    assert "finiteNumber(b.median_minutes)==null?null" in html
    assert "formatOperationalMinutes" in html
    assert "if(numeric==null)return'S/I'" in html


def test_executive_wording_is_neutral():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "La ejecutivo" not in html
    assert "El ejecutiva" not in html
    assert "La persona ejecutiva" in html
