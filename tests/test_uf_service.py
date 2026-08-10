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
    convertir_precio, completar_precio, obtener_uf_actual,
)
from chatbot.uf_migration import _clasificar

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
