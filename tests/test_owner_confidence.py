"""
Tests for the owner confidence display column and price display.
"""
import sys, os, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.chdir(os.path.join(os.path.dirname(__file__), '..'))

from owner_confidence import build_owner_confidence_doc, resolve_price_display


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
    assert "expl\u00edcito" in r["owner_confidence_display"]
    assert r["owner_confidence_sort"] == 101
    assert r["owner_confidence_type"] == "explicit"


def test_incierto_no_confidence():
    """INCIERTO → '—', sort=-1, type=unknown"""
    doc = build_doc("INCIERTO", "VALID", 0.5)
    r = build_owner_confidence_doc(doc)
    assert r["owner_confidence_display"] == "\u2014"
    assert r["owner_confidence_sort"] == -1
    assert r["owner_confidence_type"] == "unknown"


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
