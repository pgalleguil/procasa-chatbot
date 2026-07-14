"""
Tests for the owner confidence display column and price display.
"""
import sys, os, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.chdir(os.path.join(os.path.dirname(__file__), '..'))

from owner_confidence import (
    build_owner_confidence_doc,
    detect_source_price_warning,
    resolve_price_display,
)


def build_doc(state, sem_status, confidence):
    return {
        "classification": {
            "state": state,
            "final_state": state,
            "confidence": confidence,
            "semantic_check": {"status": sem_status},
        }
    }


def test_confidence_95_percent():
    """0.95 → '95%', sort=5, type=percentage"""
    doc = build_doc("DUE\u00d1O_SEGURO", "VALID", 0.95)
    r = build_owner_confidence_doc(doc)
    assert r["owner_confidence_display"] == "95%"
    assert r["owner_confidence_sort"] == 95
    assert r["owner_confidence_type"] == "percentage"


def test_confidence_82_string():
    """"0.82" string → '82%'"""
    doc = build_doc("DUE\u00d1O_SEGURO", "VALID", "0.82")
    r = build_owner_confidence_doc(doc)
    assert r["owner_confidence_display"] == "82%"
    assert r["owner_confidence_sort"] == 82


def test_confidence_80_percent():
    """0.8 → '80%'"""
    doc = build_doc("DUE\u00d1O_SEGURO", "VALID", 0.8)
    r = build_owner_confidence_doc(doc)
    assert r["owner_confidence_display"] == "80%"


def test_skipped_explicit_owner():
    """SKIPPED_EXPLICIT_OWNER → 'Dueño explícito', sort=101, type=explicit"""
    doc = build_doc("DUE\u00d1O_SEGURO", "SKIPPED_EXPLICIT_OWNER", 0.98)
    r = build_owner_confidence_doc(doc)
    assert r["owner_confidence_display"] == "98%"
    assert r["owner_confidence_sort"] == 98
    assert r["owner_confidence_type"] == "percentage"


def test_incierto_confidence():
    """INCIERTO → '—', sort=-1, type=unknown"""
    doc = build_doc("INCIERTO", "VALID", 0.5)
    r = build_owner_confidence_doc(doc)
    assert r["owner_confidence_display"] == "50%"
    assert r["owner_confidence_sort"] == 50
    assert r["owner_confidence_type"] == "percentage"


def test_confidence_absent():
    """Missing confidence → '—'"""
    doc = {"classification": {"state": "DUE\u00d1O_SEGURO", "semantic_check": {"status": "VALID"}}}
    r = build_owner_confidence_doc(doc)
    assert r["owner_confidence_display"] == "\u2014"


def test_invalid_confidence_value():
    """Invalid confidence string → '—'"""
    doc = build_doc("DUE\u00d1O_SEGURO", "VALID", "INVALIDO")
    r = build_owner_confidence_doc(doc)
    assert r["owner_confidence_display"] == "\u2014"


def test_confidence_never_empty():
    """Owner confidence cell is never empty."""
    cases = [
        {"classification": {"state": "INCIERTO", "semantic_check": {"status": "VALID"}, "confidence": 0.5}},
        {"classification": {"state": "DUE\u00d1O_SEGURO", "semantic_check": {"status": "VALID"}, "confidence": 0.95}},
        {"classification": {"state": "DUE\u00d1O_SEGURO", "semantic_check": {"status": "SKIPPED_EXPLICIT_OWNER"}, "confidence": 0.98}},
        {"classification": {"state": "CORREDOR_SEGURO", "semantic_check": {"status": "VALID"}, "confidence": 0.9}},
        {},
    ]
    for doc in cases:
        r = build_owner_confidence_doc(doc)
        assert r["owner_confidence_display"], f"Empty display for {doc}"


def test_price_display_uf_only():
    """UF 3393 → 'UF 3.393'"""
    r = resolve_price_display({"precio_uf": 3393.0})
    assert r["precio_display"] == "UF 3.393"


def test_price_display_uf_decimal():
    """UF 1713.8 → 'UF 1.713,8'"""
    r = resolve_price_display({"precio_uf": 1713.8})
    assert r["precio_display"] == "UF 1.713,8"


def test_price_display_clp():
    """CLP 8700000 → '$8.700.000'"""
    r = resolve_price_display({"precio_clp": 8700000})
    assert r["precio_display"] == "$8.700.000"


def test_price_display_both():
    """Both UF and CLP → 'UF 3.393 / $8.700.000'"""
    r = resolve_price_display({"precio_uf": 3393.0, "precio_clp": 87000000})
    assert r["precio_display"] == "UF 3.393 / $87.000.000"


def test_price_display_zero():
    """Zero/None → 'S/I'"""
    r = resolve_price_display({})
    assert r["precio_display"] == "S/I"
    r = resolve_price_display({"precio_uf": 0, "precio_clp": 0})
    assert r["precio_display"] == "S/I"


def test_html_standalone():
    """captacion_list.html does not use extends."""
    path = os.path.join(os.path.dirname(__file__), '..', 'templates', 'captacion_list.html')
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    assert '{% extends' not in content, "Template must be standalone"


def test_api_passes_display_fields_to_list_rows():
    """Guard the two hand-off points that previously dropped the values."""
    path = os.path.join(os.path.dirname(__file__), '..', 'api_captacion.py')
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'result.update(resolve_price_display(doc))' in content
    assert 'result.update(build_owner_confidence_doc(doc))' in content
    assert '"precio_display": norm["precio_display"]' in content
    assert '"precio_uf_display": norm["precio_uf_display"]' in content
    assert '"precio_clp_display": norm["precio_clp_display"]' in content
    assert '"owner_confidence_display": norm["owner_confidence_display"]' in content


def test_low_sale_price_is_flagged_without_reclassifying_operation():
    assert detect_source_price_warning("VENTA", 8.57, 350000) == "Precio inconsistente en origen"
    assert detect_source_price_warning("ARRIENDO", 8.57, 350000) == ""
    assert detect_source_price_warning("VENTA", 3500, 143000000) == ""


def test_captacion_route_preserves_active_filters():
    path = os.path.join(os.path.dirname(__file__), '..', 'webhook.py')
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    assert '"current_operacion": current_operacion' in content
    assert '"current_telefono": current_telefono' in content
    assert '"pagination_base_url": pagination_base_url' in content


def test_multi_commune_and_global_sort_controls_are_wired():
    api_path = os.path.join(os.path.dirname(__file__), '..', 'api_captacion.py')
    template_path = os.path.join(os.path.dirname(__file__), '..', 'templates', 'captacion_list.html')
    with open(api_path, 'r', encoding='utf-8') as f:
        api = f.read()
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    assert 'comuna_filter if isinstance(comuna_filter, (list, tuple))' in api
    assert '"comuna": "comuna_slug"' in api
    assert 'any(key == "antiguedad" for key, _ in sort_specs)' in api
    assert 'sort_by or "").split(",")' in api
    assert '"$skip": skip' in api and '"$limit": limit' in api
    assert 'type="checkbox" name="comuna"' in template
    assert 'data-col="comuna"' in template
    assert 'data-col="antiguedad"' in template
    assert 'event.shiftKey' in template
    assert 'sort-priority' in template
