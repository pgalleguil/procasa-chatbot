"""Original Prop360 bitacora takes precedence over later synthetic snapshots."""
from datetime import datetime, timezone

import mongomock

from chatbot.price_drop_campaign_discovery import discover_price_drops

NOW = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)


def _property(db, code="6370", old=1690, current=1654, currency="UF",
              entries=None, history=None):
    db["universo_cartera_prop360"].insert_one({
        "codigo": code,
        "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "ubicacion": {"comuna": "Maipú"},
        "tipo_operacion": {
            "venta": True, "arriendo": False, "tipo": "Departamento",
            "precio_venta": {"moneda_publicada": currency,
                             "precio_publicado": current},
        },
        "publicaciones": {"procasa": {"publicaciones": {"venta": {
            "publicada": True, "estado": "ACTIVA", "operacion": "venta",
            "url": f"https://www.procasa.cl/{code}",
        }}}},
        "bitacora": entries or [],
        "historial_cambios": history or [],
    })


def test_bitacora_executed_decrease_without_any_central_or_embedded_history():
    db = mongomock.MongoClient()["URLS"]
    _property(db, entries=[{
        "valor": "Baja el precio de venta, de UF 1.690 a UF 1.654",
        "fecha": "20/10/2026 10:38:34",
    }])
    rows = discover_price_drops(db, now=NOW)
    assert len(rows) == 1
    event = rows[0]
    assert event.property_code == "6370"
    assert (event.old_price, event.new_price, event.currency) == (1690, 1654, "UF")
    assert event.dropped_at.astimezone(timezone.utc) == datetime(
        2026, 10, 20, 13, 38, 34, tzinfo=timezone.utc)


def test_a_request_to_lower_price_does_not_create_campaign_event():
    db = mongomock.MongoClient()["URLS"]
    _property(db, entries=[{
        "valor": "Solicita bajar el precio de venta, de UF 1.690 a UF 1.654.",
        "fecha": "19/10/2026 18:35:36",
    }])
    assert discover_price_drops(db, now=NOW) == []


def test_original_dated_property_change_overrides_later_central_snapshot():
    db = mongomock.MongoClient()["URLS"]
    _property(db, code="6769", old=575991992, current=547192392,
              currency="CLP", history=[{
                  "fecha": "2026-10-07T11:25:51-03:00",
                  "campo": "precio_clp",
                  "valor_anterior": 575991992,
                  "valor_nuevo": 547192392,
                  "fuente": "prop360_bitacora",
                  "evidencia": "Baja el precio de venta",
              }])
    db["universo_cartera_prop360_historial"].insert_one({
        "codigo": "6769", "campo": "precio_publicado",
        "fecha": "2026-10-14T11:52:43+00:00",
        "moneda_anterior": "CLP", "moneda_nueva": "CLP",
        "monto_anterior": 575991992, "monto_nuevo": 547192392,
    })
    rows = discover_price_drops(db, now=NOW)
    assert len(rows) == 1
    assert rows[0].dropped_at == datetime(
        2026, 10, 7, 14, 25, 51, tzinfo=timezone.utc)


def test_bitacora_wrong_price_or_inactive_property_does_not_create_event():
    db = mongomock.MongoClient()["URLS"]
    _property(db, entries=[{
        "valor": "Baja el precio de venta, de UF 1.690 a UF 1.500",
        "fecha": "20/10/2026 10:38:34",
    }])
    assert discover_price_drops(db, now=NOW) == []
