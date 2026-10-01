"""Tests del servicio común de UF (BUG E).

Cubre:
  1. convertir_precio: regla absoluta CLP->UF / UF->CLP.
  2. completar_precio: metadata + derivado; original jamás cambia.
  3. Sin UF cache válida: guarda original sin derivado.
  4. Idempotencia: re-ejecutar con misma UF no altera el original ni duplica.
  5. obtener_uf_actual: validación de serie (no serie[0] ciego).

Uso: python -m pytest tests/test_uf_service.py -q
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.uf_service import (
    convertir_precio, completar_precio, obtener_uf_actual, validar_uf_vigente,
)
from chatbot.uf_migration import _clasificar
from scraping_convecta.scraping_prop360_ficha_completa import (
    _detect_print_price,
    canonicalize_prices,
    parse_tipo_operacion,
    build_doc,
    clean_price_uf,
    _merge_price_operation,
)

UF = 40846.11


# ─── convertir_precio ─────────────────────────────────────────────────────────

def test_clp_original_preservado_uf_derivado():
    uf, clp = convertir_precio("CLP", 85_000_000, UF)
    assert clp == 85_000_000  # ORIGINAL intacto
    assert abs(uf - round(85_000_000 / UF, 1)) < 0.01  # derivado


def test_uf_original_preservado_clp_derivado():
    uf, clp = convertir_precio("UF", 5000, UF)
    assert uf == 5000  # ORIGINAL intacto
    assert abs(clp - int(round(5000 * UF))) <= 1  # derivado


def test_sin_uf_valor_no_convierte():
    assert convertir_precio("CLP", 85_000_000, None) == (None, None)
    assert convertir_precio("CLP", 85_000_000, 0) == (None, None)


def test_precio_invalido_no_convierte():
    assert convertir_precio("CLP", None, UF) == (None, None)
    assert convertir_precio("CLP", 0, UF) == (None, None)


# ─── completar_precio (metadata) ─────────────────────────────────────────────

def test_completar_clp_genera_uf_y_metadata():
    out = completar_precio({"precio_clp": 85_000_000}, UF, "2026-08-10")
    assert out["precio_clp"] == 85_000_000
    assert out["precio_uf"] == round(85_000_000 / UF, 1)
    assert out["moneda_publicada"] == "CLP"
    assert out["precio_publicado"] == 85_000_000.0
    assert out["uf_valor_conversion"] == UF
    assert out["uf_fecha_conversion"] == "2026-08-10"
    assert out["precio_derivado"] == out["precio_uf"]
    assert out["precio_derivado_moneda"] == "UF"


def test_completar_uf_genera_clp_y_metadata():
    out = completar_precio({"precio_uf": 5000}, UF, "2026-08-10")
    assert out["precio_uf"] == 5000
    assert out["precio_clp"] == int(round(5000 * UF))
    assert out["moneda_publicada"] == "UF"
    assert out["precio_publicado"] == 5000.0
    assert out["precio_derivado_moneda"] == "CLP"


def test_sin_uf_cache_guarda_original_sin_derivado():
    out = completar_precio({"precio_clp": 85_000_000}, None, "")
    assert out == {"precio_clp": 85_000_000}  # original conservado, sin derivado


def test_metadata_previa_no_rederiva_desde_derivado():
    # 2a corrida: doc ya con ambas + metadata. Debe re-derivar desde ORIGINAL.
    doc = completar_precio({"precio_clp": 85_000_000}, UF, "2026-08-10")
    out2 = completar_precio(doc, UF, "2026-08-10")
    assert out2["precio_clp"] == 85_000_000  # original intacto
    assert out2["precio_uf"] == doc["precio_uf"]  # mismo derivado (sin drift)


# ─── _clasificar (migración) ────────────────────────────────────────────────

def test_clasificar_moneda_por_metadata_manda():
    # Metadata previa: CLP publicado, aunque ahora tenga ambas divisas.
    m, p = _clasificar(2081.0, 85_000_000,
                       {"moneda_publicada": "CLP", "precio_publicado": 85_000_000})
    assert m == "CLP" and p == 85_000_000


def test_clasificar_ambos_sin_metadata_indeterminado():
    m, p = _clasificar(2081.0, 85_000_000, None)
    assert m is None and p is None


def test_clasificar_solo_uf_infiere_uf():
    m, p = _clasificar(5000, None, None)
    assert m == "UF" and p == 5000


def test_clasificar_solo_clp_infiere_clp():
    m, p = _clasificar(None, 85_000_000, None)
    assert m == "CLP" and p == 85_000_000


# ─── Prop360 contractual currency regression ────────────────────────────────

def test_prop360_clp_publication_preserves_original_and_derives_fresh_uf():
    out = completar_precio(
        {"precio_clp": 140_000_000, "moneda_publicada": "CLP",
         "precio_publicado": 140_000_000},
        41_065.38, "2026-10-01", "mindicador.cl",
    )
    assert out["moneda_publicacion"] == "CLP"
    assert out["precio_publicado_original"] == 140_000_000
    assert out["precio_clp"] == 140_000_000
    assert out["precio_uf"] == round(140_000_000 / 41_065.38, 1)
    assert out["fuente_conversion"] == "mindicador.cl"


def test_prop360_uf_publication_keeps_full_original_precision():
    out = completar_precio(
        {"precio_uf": 118.03, "moneda_publicada": "UF",
         "precio_publicado": 118.03},
        41_065.38, "2026-10-01", "mindicador.cl",
    )
    assert out["moneda_publicacion"] == "UF"
    assert out["precio_publicado_original"] == 118.03
    assert out["precio_uf"] == 118.03
    assert out["precio_clp"] == round(118.03 * 41_065.38)


def test_prop360_form_currency_radio_is_authoritative():
    html = '''<input id="tbPrecioVenta" value="118,03">
    <input id="rbDiv1" type="radio" name="divisa" value="CLP">
    <input id="rbDiv2" type="radio" name="divisa" value="UF" checked="checked">'''
    price = parse_tipo_operacion(html, {})["precio_venta"]
    assert price["moneda_publicada"] == "UF"
    assert price["precio_publicado_original"] == 118.03
    assert price["precio_uf"] == 118.03
    assert "precio_clp" not in price


def test_prop360_form_without_explicit_currency_is_not_guessed():
    price = parse_tipo_operacion(
        '<input id="tbPrecioVenta" value="140000000">', {}
    )["precio_venta"]
    assert price["moneda_publicada"] is None
    assert "precio_clp" not in price
    assert "precio_uf" not in price


def test_prop360_print_price_with_ambiguous_decimal_comma_is_rejected():
    assert _detect_print_price("$ 68,55") == (None, None)


def test_prop360_unknown_currency_is_not_converted():
    doc = {"tipo_operacion": {"precio_venta": {
        "precio_publicado_original": "140000000", "moneda_publicada": None,
    }}}
    canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"})
    price = doc["tipo_operacion"]["precio_venta"]
    assert price["moneda_publicada"] is None
    assert "precio_clp" not in price
    assert "precio_uf" not in price
    assert price["revision_precio"]["status"] == "needs_review"


def test_clp_base_does_not_change_when_uf_rate_changes():
    price = {"precio_clp": 140_000_000, "moneda_publicada": "CLP",
             "precio_publicado_original": 140_000_000}
    first = completar_precio(price, 40_000, "2026-10-01")
    second = completar_precio(first, 41_065.38, "2026-10-01")
    assert first["precio_clp"] == second["precio_clp"] == 140_000_000
    assert first["precio_uf"] != second["precio_uf"]


def test_uf_base_does_not_change_when_uf_rate_changes():
    price = {"precio_uf": 118.037, "moneda_publicada": "UF",
             "precio_publicado_original": 118.037}
    first = completar_precio(price, 40_000, "2026-10-01")
    second = completar_precio(first, 41_065.38, "2026-10-01")
    assert first["precio_uf"] == second["precio_uf"] == 118.037
    assert first["precio_clp"] != second["precio_clp"]


def test_editable_uf_parser_preserves_all_entered_decimal_precision():
    assert clean_price_uf("118,037") == 118.037


def test_printable_pair_of_clp_and_uf_is_observation_not_currency_evidence():
    assert _detect_print_price("UF 3.427,28 $ 140.000.000") == (None, None)


def test_build_doc_does_not_use_listing_currency_when_editable_is_ambiguous():
    parsed_price = parse_tipo_operacion('<input id="tbPrecioVenta" value="140000000">', {})["precio_venta"]
    doc = build_doc("17005", {"estado": "Activa", "operacion": "Venta",
                                "precio": "UF 3.427,28 $ 140.000.000"}, {
        "tipo_operacion": {"precio_venta": parsed_price}, "estado": {},
        "datos_propietario": {}, "ubicacion": {}, "caracteristicas": {},
        "observaciones": {}, "publicaciones": {}, "bitacora": [], "metadata": {},
    })
    assert doc["resumen"]["moneda_publicacion"] is None
    assert doc["resumen"]["monto_publicacion"] is None
    assert doc["resumen"]["precio_clp"] is None
    assert doc["resumen"]["precio_uf"] is None


def test_invalid_uf_does_not_create_a_conversion():
    doc = {"tipo_operacion": {"precio_venta": {
        "precio_clp": 140_000_000, "moneda_publicada": "CLP",
        "precio_publicado_original": 140_000_000,
    }}}
    canonicalize_prices(doc, {"valor": float("nan"), "fecha": "2026-10-01"},
                        fecha_esperada="2026-10-01")
    price = doc["tipo_operacion"]["precio_venta"]
    assert price["precio_clp"] == 140_000_000
    assert "precio_uf" not in price
    assert price["revision_precio"]["status"] == "conversion_pending"


def test_stale_uf_date_skips_conversion_and_preserves_source_amount():
    doc = {"tipo_operacion": {"precio_venta": {
        "precio_clp": 140_000_000, "moneda_publicada": "CLP",
        "precio_publicado_original": 140_000_000,
    }}}
    canonicalize_prices(doc, {"valor": 41_000, "fecha": "2026-09-30T03:00:00+00:00"},
                        fecha_esperada="2026-10-01")
    price = doc["tipo_operacion"]["precio_venta"]
    assert price["precio_clp"] == 140_000_000
    assert "precio_uf" not in price
    assert price["revision_precio"]["reason"] == "uf_date_stale_or_mismatched"
    assert not validar_uf_vigente({"valor": 41_000, "fecha": "2026-09-30"}, "2026-10-01")[0]


def test_ambiguous_currency_preserves_existing_base_for_manual_review():
    existing = {"precio_uf": 100.0, "precio_clp": 4_000_000,
                "moneda_publicada": "UF", "precio_publicado_original": 100.0}
    incoming = {"precio_publicado_original": "100000000", "moneda_publicada": None,
                "fuente_moneda_publicacion": "form_currency_unresolved"}
    merged = _merge_price_operation(existing, incoming, uf_ok=True, uf_reason="current")
    assert merged["precio_uf"] == 100.0
    assert merged["precio_clp"] == 4_000_000
    assert merged["revision_precio"]["status"] == "needs_review"
    assert merged["precio_observado_pendiente"]["monto"] == "100000000"


def test_stale_uf_does_not_replace_existing_price_base():
    existing = {"precio_clp": 140_000_000, "precio_uf": 3500.0,
                "moneda_publicada": "CLP", "precio_publicado_original": 140_000_000}
    incoming = {"precio_clp": 150_000_000, "moneda_publicada": "CLP",
                "precio_publicado_original": 150_000_000}
    merged = _merge_price_operation(existing, incoming, uf_ok=False,
                                    uf_reason="uf_date_stale_or_mismatched")
    assert merged["precio_clp"] == 140_000_000
    assert merged["precio_observado_pendiente"]["monto"] == 150_000_000


def test_real_contractual_price_change_uses_new_explicit_base():
    old = {"precio_clp": 100_000_000, "moneda_publicada": "CLP",
           "precio_publicado_original": 100_000_000}
    observed = {"precio_clp": 120_000_000, "moneda_publicada": "CLP",
                "moneda_publicacion": "CLP", "precio_publicado_original": 120_000_000}
    merged = _merge_price_operation(old, observed, uf_ok=True, uf_reason="current")
    out = completar_precio(merged, 41_065.38, "2026-10-01", "mindicador.cl")
    assert out["precio_clp"] == 120_000_000
    assert out["precio_uf"] == round(120_000_000 / 41_065.38, 1)


def test_migrated_document_compatibility_preserves_contractual_base():
    migrated = {
        "precio_clp": 140_000_000, "precio_uf": 3409.2,
        "moneda_publicada": "CLP", "moneda_publicacion": "CLP",
        "precio_publicado": 140_000_000, "precio_publicado_original": 140_000_000,
        "uf_valor_conversion": 41_065.38,
    }
    updated = completar_precio(migrated, 41_200, "2026-10-02", "mindicador.cl")
    assert updated["moneda_publicada"] == "CLP"
    assert updated["precio_clp"] == migrated["precio_clp"]
    assert updated["precio_publicado_original"] == migrated["precio_publicado_original"]
    assert updated["precio_uf"] == round(140_000_000 / 41_200, 1)


def test_second_price_conversion_is_idempotent():
    initial = {"precio_uf": 118.037, "moneda_publicada": "UF",
               "precio_publicado_original": 118.037}
    first = completar_precio(initial, 41_065.38, "2026-10-01", "mindicador.cl")
    second = completar_precio(first, 41_065.38, "2026-10-01", "mindicador.cl")
    assert second == first


# ─── obtener_uf_actual (validación de serie) ────────────────────────────────

def test_obtener_uf_actual_no_usa_serie0_ciego(monkeypatch):
    """Si serie[0] es inválido pero un registro posterior es válido, elige el válido."""
    import json

    class R:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({"serie": [
                {"fecha": "2026-08-11T04:00:00.000Z", "valor": 0},  # inválido (<=0)
                {"fecha": "2026-08-10T04:00:00.000Z", "valor": 40846.11},
            ]}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=8: R())
    res = obtener_uf_actual(timeout=5)
    assert res is not None
    assert res["valor"] == 40846.11
    assert res["fuente"] == "mindicador.cl"


def test_obtener_uf_actual_serie_vacia_devuelve_none(monkeypatch):
    import json

    class R:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({"serie": []}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=8: R())
    assert obtener_uf_actual(timeout=5) is None
