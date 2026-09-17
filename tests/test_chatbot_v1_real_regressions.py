import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from chatbot.property_lookup import (  # noqa: E402
    guard_resolved_property_identity_response,
    is_property_reference_only,
    lookup_property_link,
)


YAPO_URL = (
    "https://www.yapo.cl/bienes-raices-venta-de-propiedades-apartamentos/"
    "departamento-en-venta-de-1-dorm-en-nunoa/32900652"
)


def test_yapo_external_id_resolves_one_stable_property_identity():
    import mongomock

    db = mongomock.MongoClient().get_database("property_identity")
    db.universo_cartera_prop360.insert_one({
        "codigo": "17176",
        "disponible_prop360": True,
        "tipo_operacion": {"tipo": "Departamento", "venta": True},
        "ubicacion": {"comuna": "Ñuñoa"},
        "publicaciones": {"yapo": {"url_yapo": YAPO_URL}},
    })

    prop, meta = lookup_property_link(db, YAPO_URL, "universo_cartera_prop360")

    assert prop["codigo"] == "17176"
    assert prop["ubicacion"]["comuna"] == "Ñuñoa"
    assert meta["external_id"] == "32900652"
    assert meta["operation"] == "venta"


def test_resolved_property_cannot_be_downgraded_to_not_in_portfolio():
    response = guard_resolved_property_identity_response(
        "Ese aviso no es una propiedad de nuestro portafolio actual.",
        {"codigo": "17176", "disponible_prop360": True},
        external_id="32900652",
    )

    assert "no es una propiedad" not in response.lower()
    assert "17176" in response
    assert "32900652" in response
    assert "ficha figura activa" in response.lower()


def test_url_alone_is_not_visit_confirmation():
    assert is_property_reference_only(YAPO_URL)
    assert is_property_reference_only(f"{YAPO_URL} disponible?")
    assert not is_property_reference_only(f"{YAPO_URL} quiero visitarla mañana")


def test_message_sent_events_never_enter_customer_inbound_pipeline():
    source = Path(__file__).parents[1].joinpath("webhook.py").read_text(encoding="utf-8")
    assert '"message.sent", "messages.sent"' in source
    assert '"status": "non_inbound_event_ignored"' in source
    assert "create_inbound_job" in source
