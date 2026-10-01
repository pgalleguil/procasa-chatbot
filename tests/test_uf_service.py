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
from chatbot.uf_migration import migrar as legacy_uf_migrator
import mongomock
from scraping_convecta.scraping_prop360_ficha_completa import (
    _detect_print_price,
    canonicalize_prices,
    parse_tipo_operacion,
    parse_ficha_imprimible,
    build_doc,
    clean_price_uf,
    _merge_price_operation,
    upsert_ficha,
    build_arg_parser,
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
    original_price = {
        "precio_publicado_original": "140000000", "moneda_publicada": None,
        "fuente_moneda_publicacion": "form_currency_unresolved",
        "price_base_verified": False,
    }
    doc = {"tipo_operacion": {"precio_venta": original_price}}
    canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"})
    price = doc["tipo_operacion"]["precio_venta"]
    assert price == original_price
    assert doc["metadata"]["price_verification"] == {
        "status": "PRICE_VERIFICATION_REQUIRED",
        "reason": "CURRENCY_UNKNOWN",
        "current_base_price_verified": False,
        "operations": ["precio_venta"],
    }


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


def test_printable_single_explicit_price_is_observation_not_contract_base():
    parsed = parse_ficha_imprimible(
        '<h4 class="text-right">Venta <label>UF 118,037</label></h4>'
    )
    price = parsed["tipo_operacion"]["precio_venta"]
    assert price.get("precio_uf") is None
    assert price.get("precio_clp") is None
    assert price.get("moneda_publicada") is None
    assert price["precio_observado_imprimible"]["monto_observado"] == 118.037


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
        "moneda_publicacion": "CLP",
        "fuente_moneda_publicacion": "prop360_editable_currency_radio",
        "price_base_verified": True,
    }}}
    canonicalize_prices(doc, {"valor": float("nan"), "fecha": "2026-10-01"},
                        fecha_esperada="2026-10-01")
    price = doc["tipo_operacion"]["precio_venta"]
    assert price["precio_clp"] == 140_000_000
    assert "precio_uf" not in price
    assert "revision_precio" not in price
    assert doc["metadata"]["price_verification"]["reason"] == "uf_value_invalid"


def test_stale_uf_date_skips_conversion_and_preserves_source_amount():
    doc = {"tipo_operacion": {"precio_venta": {
        "precio_clp": 140_000_000, "moneda_publicada": "CLP",
        "precio_publicado_original": 140_000_000,
        "moneda_publicacion": "CLP",
        "fuente_moneda_publicacion": "prop360_editable_currency_radio",
        "price_base_verified": True,
    }}}
    canonicalize_prices(doc, {"valor": 41_000, "fecha": "2026-09-30T03:00:00+00:00"},
                        fecha_esperada="2026-10-01")
    price = doc["tipo_operacion"]["precio_venta"]
    assert price["precio_clp"] == 140_000_000
    assert "precio_uf" not in price
    assert doc["metadata"]["price_verification"]["reason"] == "uf_date_stale_or_mismatched"
    assert not validar_uf_vigente({"valor": 41_000, "fecha": "2026-09-30"}, "2026-10-01")[0]


def test_ambiguous_currency_preserves_existing_base_for_manual_review():
    existing = {"precio_uf": 100.0, "precio_clp": 4_000_000,
                "moneda_publicada": "UF", "precio_publicado_original": 100.0,
                "uf_valor_conversion": 40_000, "uf_fecha_conversion": "2026-08-10"}
    incoming = {"precio_publicado_original": "100000000", "moneda_publicada": None,
                "fuente_moneda_publicacion": "form_currency_unresolved"}
    merged = _merge_price_operation(existing, incoming, uf_ok=True, uf_reason="current")
    assert merged == existing
    doc = {"tipo_operacion": {"precio_venta": merged}}
    canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"})
    assert doc["tipo_operacion"]["precio_venta"] == existing
    assert doc["metadata"]["price_verification"]["reason"] == "CURRENCY_UNKNOWN"


def test_stale_uf_does_not_replace_existing_price_base():
    existing = {"precio_clp": 140_000_000, "precio_uf": 3500.0,
                "moneda_publicada": "CLP", "precio_publicado_original": 140_000_000,
                "uf_valor_conversion": 40_000, "uf_fecha_conversion": "2026-08-10"}
    incoming = {"precio_clp": 150_000_000, "moneda_publicada": "CLP",
                "moneda_publicacion": "CLP", "precio_publicado_original": 150_000_000,
                "fuente_moneda_publicacion": "prop360_editable_currency_radio",
                "price_base_verified": True}
    merged = _merge_price_operation(existing, incoming, uf_ok=False,
                                    uf_reason="uf_date_stale_or_mismatched")
    assert merged == existing


def test_real_contractual_price_change_uses_new_explicit_base():
    old = {"precio_clp": 100_000_000, "moneda_publicada": "CLP",
           "precio_publicado_original": 100_000_000}
    observed = {"precio_clp": 120_000_000, "moneda_publicada": "CLP",
                "moneda_publicacion": "CLP", "precio_publicado_original": 120_000_000,
                "fuente_moneda_publicacion": "prop360_editable_currency_radio",
                "price_base_verified": True}
    merged = _merge_price_operation(old, observed, uf_ok=True, uf_reason="current")
    out = completar_precio(merged, 41_065.38, "2026-10-01", "mindicador.cl")
    assert out["precio_clp"] == 120_000_000
    assert out["precio_uf"] == round(120_000_000 / 41_065.38, 1)


def test_verified_current_clp_base_recalculates_uf_after_rate_change():
    incoming = {
        "precio_clp": 140_000_000, "moneda_publicada": "CLP",
        "moneda_publicacion": "CLP", "precio_publicado_original": 140_000_000,
        "fuente_moneda_publicacion": "prop360_editable_currency_radio",
        "price_base_verified": True,
    }
    doc = {"tipo_operacion": {"precio_venta": incoming}}
    canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"},
                        fecha_esperada="2026-10-01")
    assert doc["tipo_operacion"]["precio_venta"]["precio_clp"] == 140_000_000
    assert doc["tipo_operacion"]["precio_venta"]["precio_uf"] == 3409.2


def test_verified_current_uf_base_recalculates_clp_after_rate_change():
    incoming = {
        "precio_uf": 118.037, "moneda_publicada": "UF",
        "moneda_publicacion": "UF", "precio_publicado_original": 118.037,
        "fuente_moneda_publicacion": "prop360_editable_currency_radio",
        "price_base_verified": True,
    }
    doc = {"tipo_operacion": {"precio_venta": incoming}}
    canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"},
                        fecha_esperada="2026-10-01")
    price = doc["tipo_operacion"]["precio_venta"]
    assert price["precio_uf"] == 118.037
    assert price["precio_clp"] == round(118.037 * 41_065.38)


def test_historical_clp_without_current_verification_is_byte_value_preserved():
    historical = {
        "precio_clp": 12_254_622, "precio_uf": 300,
        "moneda_publicada": "CLP", "precio_publicado_original": 12_254_622,
        "uf_valor_conversion": 40_846.11,
        "uf_fecha_conversion": "2026-08-10T04:00:00+00:00",
        "precio_derivado": 300, "precio_derivado_moneda": "UF",
    }
    merged = _merge_price_operation(historical, {
        "precio_observado_imprimible": {"unidad_indicada": "UF", "monto_observado": 300},
    }, uf_ok=True, uf_reason="current")
    doc = {"tipo_operacion": {"precio_venta": merged}}
    before = dict(historical)
    canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"})
    assert doc["tipo_operacion"]["precio_venta"] == before
    assert doc["metadata"]["price_verification"]["status"] == "PRICE_VERIFICATION_REQUIRED"
    assert doc["metadata"]["price_verification"]["reason"] == "CURRENCY_UNKNOWN"


def test_historical_uf_without_current_verification_is_preserved_exactly():
    historical = {
        "precio_uf": 132.57, "precio_clp": 5_444_037,
        "moneda_publicada": "UF", "precio_publicado_original": 132.57,
        "uf_valor_conversion": 40_846.11,
        "uf_fecha_conversion": "2026-08-10T04:00:00+00:00",
        "precio_derivado": 5_444_037, "precio_derivado_moneda": "CLP",
    }
    merged = _merge_price_operation(historical, {"precio_uf": None}, uf_ok=True,
                                    uf_reason="current")
    doc = {"tipo_operacion": {"precio_arriendo": merged}}
    canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"})
    assert doc["tipo_operacion"]["precio_arriendo"] == historical


def test_printable_uf_300_with_inaccessible_editable_skips_price_write():
    parsed = parse_ficha_imprimible(
        '<h4 class="text-right">Venta <label>UF 300</label></h4>'
    )["tipo_operacion"]["precio_venta"]
    historical = {"precio_clp": 12_254_622, "precio_uf": 300,
                  "moneda_publicada": "CLP", "precio_publicado_original": 12_254_622,
                  "uf_valor_conversion": 40_846.11,
                  "uf_fecha_conversion": "2026-08-10T04:00:00+00:00"}
    merged = _merge_price_operation(historical, parsed, uf_ok=True, uf_reason="current")
    doc = {"tipo_operacion": {"precio_venta": merged}}
    canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"})
    assert parsed.get("moneda_publicada") is None
    assert parsed["precio_observado_imprimible"]["monto_observado"] == 300
    assert doc["tipo_operacion"]["precio_venta"] == historical
    assert doc["metadata"]["price_verification"]["reason"] == "CURRENCY_UNKNOWN"


def test_listing_clp_uf_pair_with_inaccessible_editable_preserves_mongo():
    historical = {"precio_clp": 12_254_622, "precio_uf": 300,
                  "moneda_publicada": "CLP", "precio_publicado_original": 12_254_622,
                  "uf_valor_conversion": 40_846.11,
                  "uf_fecha_conversion": "2026-08-10T04:00:00+00:00"}
    parsed = parse_tipo_operacion('<input id="tbPrecioVenta" value="12319614">', {})["precio_venta"]
    doc = build_doc("16346", {"estado": "Activa", "operacion": "Venta",
                               "precio": "UF 300 $ 12.319.614"}, {
        "tipo_operacion": {"precio_venta": parsed}, "estado": {},
        "datos_propietario": {}, "ubicacion": {}, "caracteristicas": {},
        "observaciones": {}, "publicaciones": {}, "bitacora": [], "metadata": {},
    })
    merged = _merge_price_operation(historical, doc["tipo_operacion"]["precio_venta"],
                                    uf_ok=True, uf_reason="current")
    price_doc = {"tipo_operacion": {"precio_venta": merged}}
    canonicalize_prices(price_doc, {"valor": 41_065.38, "fecha": "2026-10-01"})
    assert doc["resumen"]["moneda_publicacion"] is None
    assert price_doc["tipo_operacion"]["precio_venta"] == historical


def test_unknown_historical_price_is_idempotently_skipped_twice():
    historical = {"precio_clp": 12_254_622, "precio_uf": 300,
                  "moneda_publicada": "CLP", "precio_publicado_original": 12_254_622,
                  "uf_valor_conversion": 40_846.11,
                  "uf_fecha_conversion": "2026-08-10T04:00:00+00:00"}
    for _ in range(2):
        merged = _merge_price_operation(historical, {
            "fuente_moneda_publicacion": "form_currency_unresolved",
            "price_base_verified": False,
        }, uf_ok=True, uf_reason="current")
        doc = {"tipo_operacion": {"precio_venta": merged}}
        canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"})
        assert doc["tipo_operacion"]["precio_venta"] == historical


def test_code_16346_anonymized_snapshot_skips_price_update():
    historical = {"precio_clp": 12_254_622, "precio_uf": 300,
                  "moneda_publicada": "CLP", "precio_publicado_original": 12_254_622,
                  "uf_valor_conversion": 40_846.11,
                  "uf_fecha_conversion": "2026-08-10T04:00:00+00:00",
                  "precio_derivado": 300, "precio_derivado_moneda": "UF"}
    incoming = {"precio_observado_imprimible": {
        "texto": "UF 300", "unidad_indicada": "UF", "monto_observado": 300,
        "fuente": "prop360_print_primary_price_label"}}
    merged = _merge_price_operation(historical, incoming, uf_ok=True, uf_reason="current")
    doc = {"tipo_operacion": {"precio_venta": merged}}
    canonicalize_prices(doc, {"valor": 41_065.38, "fecha": "2026-10-01"})
    assert incoming.get("moneda_publicada") is None
    assert merged == historical
    assert doc["tipo_operacion"]["precio_venta"] == historical
    assert doc["metadata"]["price_verification"]["reason"] == "CURRENCY_UNKNOWN"
    assert {k: v for k, v in merged.items() if k.startswith("uf_")} == {
        "uf_valor_conversion": 40_846.11,
        "uf_fecha_conversion": "2026-08-10T04:00:00+00:00",
    }
    assert incoming.get("price_base_verified") is not True


def test_code_16346_full_upsert_path_skips_historical_price_write():
    db = mongomock.MongoClient().test_db
    coll = db.properties
    historical = {
        "precio_clp": 12_254_622, "precio_uf": 300,
        "moneda_publicada": "CLP", "precio_publicado_original": 12_254_622,
        "uf_valor_conversion": 40_846.11,
        "uf_fecha_conversion": "2026-08-10T04:00:00+00:00",
        "precio_derivado": 300, "precio_derivado_moneda": "UF",
    }
    coll.insert_one({
        "codigo": "16346",
        "tipo_operacion": {"precio_venta": dict(historical)},
        "resumen": {"precio_clp": 12_254_622, "precio_uf": 300},
        "metadata": {"source": "prior_scrape"},
    })
    parsed_price = parse_ficha_imprimible(
        '<h4 class="text-right">Venta <label>UF 300</label></h4>'
    )["tipo_operacion"]["precio_venta"]
    doc = build_doc("16346", {"estado": "Activa", "operacion": "Venta",
                               "precio": "UF 300 $ 12.319.614"}, {
        "tipo_operacion": {"precio_venta": parsed_price}, "estado": {},
        "datos_propietario": {}, "ubicacion": {}, "caracteristicas": {},
        "observaciones": {}, "publicaciones": {}, "bitacora": [], "metadata": {},
    })
    upsert_ficha(coll, doc, uf_info={
        "valor": 41_065.38, "fecha": "2026-10-01", "fuente": "mindicador.cl"
    })
    saved = coll.find_one({"codigo": "16346"})
    assert saved["tipo_operacion"]["precio_venta"] == historical
    assert saved["metadata"]["price_verification"]["status"] == "PRICE_VERIFICATION_REQUIRED"
    assert saved["metadata"]["price_verification"]["reason"] == "CURRENCY_UNKNOWN"
    first_saved_price = dict(saved["tipo_operacion"]["precio_venta"])
    upsert_ficha(coll, doc, uf_info={
        "valor": 41_065.38, "fecha": "2026-10-01", "fuente": "mindicador.cl"
    })
    saved_twice = coll.find_one({"codigo": "16346"})
    assert saved_twice["tipo_operacion"]["precio_venta"] == first_saved_price


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


def test_manual_portfolio_scraper_is_dry_run_by_default_and_execute_is_explicit():
    parser = build_arg_parser()
    assert parser.parse_args([]).execute is False
    assert parser.parse_args(["--dry-run"]).execute is False
    assert parser.parse_args(["--execute"]).execute is True


def test_manual_verified_price_change_is_audited_and_repeat_is_idempotent():
    coll = mongomock.MongoClient().test_db.properties

    def make_doc():
        return build_doc("17005", {"estado": "Activa", "operacion": "Venta",
                                   "precio": "$140.000.000"}, {
            "tipo_operacion": {"precio_venta": {
                "precio_clp": 140_000_000,
                "moneda_publicada": "CLP",
                "moneda_publicacion": "CLP",
                "precio_publicado": 140_000_000,
                "precio_publicado_original": 140_000_000,
                "fuente_moneda_publicacion": "prop360_editable_currency_radio",
                "price_base_verified": True,
            }},
            "estado": {}, "datos_propietario": {}, "ubicacion": {},
            "caracteristicas": {}, "observaciones": {}, "publicaciones": {},
            "bitacora": [], "metadata": {},
        })

    quote = {"valor": 41_065.38, "fecha": "2026-10-01", "fuente": "mindicador.cl"}
    upsert_ficha(coll, make_doc(), uf_info=quote)
    first = coll.find_one({"codigo": "17005"})
    history = first["metadata"]["manual_price_refresh_history"]
    assert len(history) == 1
    assert history[0]["codigo"] == "17005"
    assert history[0]["moneda_base"] == "CLP"
    assert history[0]["precio_base_nuevo"] == 140_000_000
    assert history[0]["precio_clp_nuevo"] == 140_000_000
    assert history[0]["precio_uf_nuevo"] == 3409.2
    assert history[0]["reason"] == "contractual_base_changed"

    writes = []
    original_update_one = coll.update_one

    def counted_update_one(*args, **kwargs):
        writes.append((args, kwargs))
        return original_update_one(*args, **kwargs)

    coll.update_one = counted_update_one
    upsert_ficha(coll, make_doc(), uf_info=quote)
    assert writes == []
    second = coll.find_one({"codigo": "17005"})
    assert len(second["metadata"]["manual_price_refresh_history"]) == 1
    assert second["tipo_operacion"]["precio_venta"]["precio_clp"] == 140_000_000


def test_legacy_price_migrator_is_disabled_even_if_execute_requested():
    class NoMongoAccess:
        def __getitem__(self, key):
            raise AssertionError("disabled migrator must not access Mongo")

    result = legacy_uf_migrator(NoMongoAccess(), 41_065.38, "2026-10-01", dry_run=False)
    assert result["status"] == "disabled"
    assert result["writes"] == 0


def test_webhook_does_not_start_uf_or_price_scheduler():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "webhook.py").read_text(encoding="utf-8")
    assert "create_task(_usl())" not in source
    assert "from chatbot.uf_sync_loop import uf_sync_loop" not in source
    assert "Automatic portfolio UF/price sync disabled" in source


def test_legacy_ficha_scheduler_is_a_no_io_disabled_stub():
    from chatbot.ficha_sync_loop import run_ficha_sync_cycle

    result = run_ficha_sync_cycle(db=object())
    assert result["status"] == "disabled"
    assert result["portfolio_reads"] == 0
    assert result["portfolio_writes"] == 0


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
