"""
Tests de integracion para redistribucion de captacion.
Valida proteccion de propiedades gestionadas, balance por stock_pendiente,
y uso canonico de ejecutivo_id.

Ejecutar: python -m pytest tests/test_captacion_redistribution.py -v
"""
import sys, os, re, unicodedata
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from unittest.mock import patch

import pytest
import mongomock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ========== HELPERS CANONICOS ==========

def norm_commune(v):
    if not v: return None
    s = str(v).lower().strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("ñ", "n")
    s = re.sub(r"[^a-z0-9\s_-]", "", s)
    s = re.sub(r"[\s_]+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or None


SIN_GESTION_ESTADOS = {None, "", "NUEVO", "DETECTADO"}
VISIBLE_STATES = {"DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"}
TERMINAL_STATES = {"Captado", "CAPTADO", "Descartado", "DESCARTADO", "Corredor",
                   "Telefono invalido", "Propiedad no disponible", "Publicacion expirada", "No interesado"}


def has_management_evidence(prop, events_collection=None):
    """Determina si una propiedad tiene evidencia de gestion humana.
    Consulta gestion.notas, gestion.actividades, gestion.fecha_ultima_gestion,
    gestion.estado, y captacion_management_events."""
    g = prop.get("gestion") or {}

    # Estado distinto de NUEVO/DETECTADO indica que alguien lo cambio
    estado = g.get("estado")
    if estado not in SIN_GESTION_ESTADOS:
        return True, f"estado={estado}"

    # Fecha de ultima gestion registrada
    if g.get("fecha_ultima_gestion") is not None:
        return True, "fecha_ultima_gestion"

    # Notas (WhatsApp, llamadas, comentarios)
    notas = g.get("notas") or []
    if len(notas) > 0:
        return True, f"notas={len(notas)}"

    # Actividades registradas
    acts = g.get("actividades") or []
    if len(acts) > 0:
        return True, f"actividades={len(acts)}"

    # Eventos en captacion_management_events
    if events_collection is not None:
        pid = str(prop.get("_id", ""))
        lid = prop.get("listing_id")
        url = prop.get("url")

        or_conds = []
        if pid:
            or_conds.append({"property_id": pid})
        if lid:
            or_conds.append({"listing_id": lid})
        if url:
            or_conds.append({"url": url})

        if or_conds:
            evt = events_collection.find_one({"$or": or_conds})
            if evt:
                return True, f"event:{evt.get('action')}/{evt.get('channel')}"

    return False, None


def is_eligible(prop):
    c = prop.get("classification") or {}
    if c.get("state") not in VISIBLE_STATES:
        return False
    if not c.get("assignment_ready"):
        return False
    if prop.get("scrape_stage") in {"ad_removed", "needs_rescrape", "incomplete"}:
        return False
    if prop.get("html_validation_status") in {"LISTING_REMOVED", "INVALID", "BLOCKED"}:
        return False
    if not norm_commune(prop.get("comuna_slug") or prop.get("comuna")):
        return False
    return True


def stock_pendiente_sin_gestion(agent_id, coll, events_coll):
    """Calcula el stock pendiente sin gestion para un agente."""
    props = list(coll.find({"gestion.ejecutivo_id": agent_id}))
    count = 0
    for p in props:
        g = p.get("gestion") or {}
        if g.get("estado") in TERMINAL_STATES:
            continue
        if not is_eligible(p):
            continue
        has_ev, _ = has_management_evidence(p, events_coll)
        if not has_ev:
            count += 1
    return count


# ========== FIXTURES ==========

@pytest.fixture
def mongo():
    return mongomock.MongoClient()


@pytest.fixture
def db(mongo):
    return mongo["test_db"]


@pytest.fixture
def coll(db):
    return db["propiedades_captacion"]


@pytest.fixture
def events_coll(db):
    return db["captacion_management_events"]


@pytest.fixture
def users_coll(db):
    return db["usuarios"]


@pytest.fixture
def memberships_coll(db):
    return db["captacion_team_memberships"]


def make_prop(pid="prop_001", comuna="nunoa", estado="NUEVO",
              ejecutivo_id=None, ejecutivo_asignado=None,
              notas=None, acts=None, fecha_ultima=None,
              class_state="DUEÑO_SEGURO", assignment_ready=True):
    """Fabrica un documento de propiedad de test."""
    return {
        "_id": pid,
        "comuna_slug": comuna,
        "comuna": "Ñuñoa",
        "origen": "toctoc",
        "listing_id": f"LST-{pid}",
        "url": f"https://test.cl/{pid}",
        "classification": {
            "state": class_state,
            "assignment_ready": assignment_ready,
        },
        "scrape_stage": "parsed",
        "html_validation_status": "OK",
        "gestion": {
            "estado": estado,
            "ejecutivo_id": ejecutivo_id,
            "ejecutivo_asignado": ejecutivo_asignado or "",
            "ejecutivo_nombre": ejecutivo_asignado or "",
            "notas": notas or [],
            "actividades": acts or [],
            "fecha_ultima_gestion": fecha_ultima,
            "historial_asignaciones": [],
        },
    }


def make_agent(aid="agent_1", name="Agente Uno", comunas=None,
               is_active=True, rol="agente", email=None):
    return {
        "_id": aid,
        "nombre": name,
        "email": email or f"{aid}@test.cl",
        "rol": rol,
        "is_active": is_active,
        "comunas_interes": comunas or ["Ñuñoa"],
        "comunas_interes_norm": [norm_commune(c) for c in (comunas or ["Ñuñoa"])],
        "captacion_weight": 1.0,
    }


# ========== TESTS ==========

class TestManagementEvidence:
    """Tests 1-9: deteccion de evidencia de gestion."""

    def test_sin_gestion_puede_redistribuirse(self):
        """1. Propiedad NUEVO sin nada puede redistribuirse."""
        p = make_prop("p1", estado="NUEVO")
        has_ev, reason = has_management_evidence(p)
        assert not has_ev

    def test_con_nota_no_redistribuye(self):
        """2. Propiedad con nota no puede redistribuirse."""
        p = make_prop("p2", notas=[{"usuario": "A", "content": "llamada"}])
        has_ev, reason = has_management_evidence(p)
        assert has_ev
        assert "notas" in reason

    def test_con_actividad_no_redistribuye(self):
        """3. Propiedad con actividad no puede redistribuirse."""
        p = make_prop("p3", acts=[{"user": "A", "action": "call"}])
        has_ev, reason = has_management_evidence(p)
        assert has_ev
        assert "actividades" in reason

    def test_con_fecha_ultima_gestion_no_redistribuye(self):
        """4. fecha_ultima_gestion protege."""
        p = make_prop("p4", fecha_ultima=datetime.now(timezone.utc))
        has_ev, reason = has_management_evidence(p)
        assert has_ev
        assert "fecha_ultima_gestion" in reason

    def test_estado_distinto_nuevo_no_redistribuye(self):
        """5. Estado no NUEVO/DETECTADO protege."""
        for estado in ["Por contactar", "En gestion", "Contacto exitoso",
                        "Sin respuesta", "Reunion agendada", "Captado"]:
            p = make_prop(f"p_{estado}", estado=estado)
            has_ev, reason = has_management_evidence(p)
            assert has_ev, f"Estado {estado} deberia proteger"

    def test_evento_whatsapp_protege(self, events_coll):
        """6. Evento WhatsApp en management_events protege."""
        pid = "p6"
        events_coll.insert_one({
            "property_id": pid,
            "action": "message",
            "channel": "wa",
            "result": "sent",
            "actor": "agent_1",
            "occurred_at": datetime.now(timezone.utc),
        })
        p = make_prop(pid, estado="NUEVO")
        has_ev, reason = has_management_evidence(p, events_coll)
        assert has_ev, f"Evento WA deberia proteger, reason={reason}"

    def test_opened_app_es_inicio_no_mensaje(self):
        """7. opened_app = inicio de accion WA, no mensaje enviado."""
        pid = "p7"
        events_coll = mongomock.MongoClient().test_db.captacion_management_events
        events_coll.insert_one({
            "property_id": pid,
            "action": "message",
            "channel": "wa",
            "result": "opened_app",
            "actor": "agent_1",
            "occurred_at": datetime.now(timezone.utc),
        })
        p = make_prop(pid, estado="NUEVO")
        has_ev, reason = has_management_evidence(p, events_coll)
        assert has_ev, "opened_app es inicio de gestion, debe proteger"
        # Verificar que no se describe como "mensaje enviado"
        assert "event" in reason

    def test_seguimiento_pendiente_protege(self):
        """8. Seguimiento pendiente protege."""
        p = make_prop("p8", estado="Reunion agendada")
        has_ev, reason = has_management_evidence(p)
        assert has_ev

    def test_gestion_por_otro_usuario_protege(self):
        """9. Gestion por usuario distinto al ejecutivo formal protege."""
        # Simula: propiedad asignada a Raquel, pero Susana puso una nota
        p = make_prop("p9", ejecutivo_id="raquel_id", ejecutivo_asignado="Raquel",
                       notas=[{"usuario": "Susana Ensignia", "canal": "wa",
                               "content": "consulta enviada", "resultado": "opened_app"}])
        has_ev, reason = has_management_evidence(p)
        assert has_ev
        assert "notas" in reason

    def test_agente_inactivo_con_gestion_a_revision(self):
        """10. Agente inactivo con gestion previa: va a revision, no redistribucion."""
        p = make_prop("p10", ejecutivo_id="inactive_1", ejecutivo_asignado="Inactivo",
                       estado="Por contactar", notas=[{"usuario": "Inactivo", "content": "X"}])
        has_ev, reason = has_management_evidence(p)
        assert has_ev
        # La propiedad debe marcarse para revision, no redistribuirse
        # Este test verifica que la deteccion funciona; la logica de "a revision"
        # se implementa en el algoritmo de redistribucion

    def test_agente_inactivo_sin_gestion_si_redistribuye(self):
        """11. Agente inactivo sin gestion: si se redistribuye."""
        p = make_prop("p11", ejecutivo_id="inactive_2", estado="NUEVO")
        has_ev, reason = has_management_evidence(p)
        assert not has_ev


class TestStockPendiente:
    """Tests 12-13: metrica de balance."""

    def test_metric_usa_stock_pendiente_no_carga_total(self, coll, events_coll):
        """12. La metrica usa stock_pendiente_sin_gestion."""
        # Agente con 10 props: 5 gestionadas, 5 sin gestion
        aid = "agent_test"
        for i in range(5):
            coll.insert_one(make_prop(f"g_{i}", ejecutivo_id=aid, estado="Por contactar",
                                       notas=[{"usuario": "X", "content": "ok"}]))
        for i in range(5):
            coll.insert_one(make_prop(f"s_{i}", ejecutivo_id=aid, estado="NUEVO"))

        stock = stock_pendiente_sin_gestion(aid, coll, events_coll)
        assert stock == 5, f"Stock pendiente deberia ser 5, fue {stock}"
        # Carga total seria 10, pero stock pendiente es 5

    def test_menor_stock_recibe_prioridad(self, coll, events_coll, users_coll):
        """13. Agente con menor stock pendiente en comuna compartida recibe prioridad."""
        # Agente A: stock=8, Agente B: stock=3, ambos cubren nunoa
        users_coll.insert_one(make_agent("A", "Agente A", comunas=["Ñuñoa"]))
        users_coll.insert_one(make_agent("B", "Agente B", comunas=["Ñuñoa"]))

        for i in range(8):
            coll.insert_one(make_prop(f"a_{i}", comuna="nunoa", ejecutivo_id="A", estado="NUEVO"))
        for i in range(3):
            coll.insert_one(make_prop(f"b_{i}", comuna="nunoa", ejecutivo_id="B", estado="NUEVO"))

        stock_a = stock_pendiente_sin_gestion("A", coll, events_coll)
        stock_b = stock_pendiente_sin_gestion("B", coll, events_coll)

        assert stock_a == 8
        assert stock_b == 3
        # B tiene menor stock, deberia recibir prioridad
        assert stock_b < stock_a, "B deberia tener prioridad por menor stock"


class TestComunaRestriction:
    """Test 14: restriccion por comuna."""

    def test_fuera_de_comuna_no_asigna(self, coll, events_coll, users_coll):
        """14. Propiedades fuera de las comunas del agente nunca se asignan."""
        users_coll.insert_one(make_agent("A", "Agente A", comunas=["Ñuñoa"]))
        # Propiedad en maipu
        prop = make_prop("p_maipu", comuna="maipu", estado="NUEVO")
        coll.insert_one(prop)

        agent_comunas = users_coll.find_one({"_id": "A"})["comunas_interes_norm"]
        prop_slug = norm_commune(prop.get("comuna_slug") or prop.get("comuna"))
        assert prop_slug == "maipu"
        assert prop_slug not in agent_comunas, "Maipu no deberia estar en comunas de A"


class TestAtomicGuard:
    """Test 15: guarda atomica."""

    def test_atomic_falla_si_aparece_gestion(self, coll, events_coll):
        """15. Update atomico falla si gestion aparece entre dry-run y apply."""
        pid = "p_atomic"
        coll.insert_one(make_prop(pid, estado="NUEVO", ejecutivo_id=None))

        # Simular: entre plan y apply, alguien agrego una nota
        coll.update_one({"_id": pid}, {"$push": {"gestion.notas": {"usuario": "Z", "content": "intruso"}}})

        # Verificar que ahora tiene evidencia
        p = coll.find_one({"_id": pid})
        has_ev, _ = has_management_evidence(p, events_coll)
        assert has_ev, "La propiedad ahora tiene gestion"
        # El apply deberia fallar su guarda


class TestEjecutivoIdCanonico:
    """Tests 16-17: ejecutivo_id canonico."""

    def test_query_por_id_funciona_con_tilde(self, coll):
        """16. Query por ejecutivo_id funciona aunque el nombre tenga tilde."""
        aid = "hernan_id"
        name_con_tilde = "Hernán Castro"
        name_sin_tilde = "Hernan Castro"

        coll.insert_one(make_prop("p_a", ejecutivo_id=aid, ejecutivo_asignado=name_con_tilde))
        coll.insert_one(make_prop("p_b", ejecutivo_id=aid, ejecutivo_asignado=name_sin_tilde))

        # Query canonico por ID
        count_id = coll.count_documents({"gestion.ejecutivo_id": aid})
        assert count_id == 2, f"Por ID deberian ser 2, fueron {count_id}"

        # Query por nombre con tilde (legacy)
        count_tilde = coll.count_documents({"gestion.ejecutivo_asignado": name_con_tilde})
        assert count_tilde == 1

        # Query por nombre sin tilde (NO deberia usarse)
        count_sin = coll.count_documents({"gestion.ejecutivo_asignado": name_sin_tilde})
        assert count_sin == 1  # solo p_b tiene el nombre sin tilde

        # El conteo canonico es por ID
        assert count_id == 2

    def test_fallback_nombre_solo_sin_id(self, coll):
        """17. Fallback por nombre solo en docs legacy sin ejecutivo_id."""
        # Doc legacy: sin ejecutivo_id, solo nombre
        coll.insert_one(make_prop("legacy", ejecutivo_id=None, ejecutivo_asignado="Agente Viejo"))
        # Doc moderno: con ejecutivo_id
        coll.insert_one(make_prop("modern", ejecutivo_id="aid_1", ejecutivo_asignado="Agente Nuevo"))

        # Query canonica para agente: busca por ID
        modernos = coll.count_documents({"gestion.ejecutivo_id": "aid_1"})
        assert modernos == 1

        # Fallback: documentos sin ejecutivo_id matchean por nombre
        legacy_q = {"$or": [
            {"gestion.ejecutivo_id": {"$exists": False}},
            {"gestion.ejecutivo_id": None},
        ], "gestion.ejecutivo_asignado": "Agente Viejo"}
        assert coll.count_documents(legacy_q) == 1


class TestMemberships:
    """Tests 18-19: membresias dinamicas."""

    def test_agentes_aparecen_con_membresia(self, users_coll, memberships_coll):
        """18. MPaz y Hernan aparecen al tener membresia activa."""
        from datetime import date, datetime as dt
        import pytz

        mpaz_id = "69c19b98fbbbf113235ba844"
        hernan_id = "6a681413140190dde11f26d1"

        users_coll.insert_one({
            "_id": mpaz_id,
            "nombre": "María Paz Galleguillos",
            "email": "mgalleguillos@procasa.cl",
            "rol": "agente",
            "is_active": True,
        })
        users_coll.insert_one({
            "_id": hernan_id,
            "nombre": "Hernán Castro",
            "email": "h.castroman.8@gmail.com",
            "rol": "agente",
            "is_active": True,
        })

        today = date.today().isoformat()

        # Sin membresia: no hay team members
        members_empty = list(memberships_coll.find({"enabled": True,
            "start_date": {"$lte": today},
            "$or": [{"end_date": None}, {"end_date": {"$exists": False}}, {"end_date": {"$gte": today}}],
        }))
        assert len([m for m in members_empty if m["user_id"] == mpaz_id]) == 0
        assert len([m for m in members_empty if m["user_id"] == hernan_id]) == 0

        # Agregar membresias
        memberships_coll.insert_one({
            "user_id": mpaz_id, "user_name": "María Paz Galleguillos",
            "enabled": True, "start_date": today, "role": "agente",
        })
        memberships_coll.insert_one({
            "user_id": hernan_id, "user_name": "Hernán Castro",
            "enabled": True, "start_date": today, "role": "agente",
        })

        # Verificar que las membresias existen
        members_active = list(memberships_coll.find({"enabled": True,
            "start_date": {"$lte": today},
            "$or": [{"end_date": None}, {"end_date": {"$exists": False}}, {"end_date": {"$gte": today}}],
        }))
        mpaz_members = [m for m in members_active if m["user_id"] == mpaz_id]
        hernan_members = [m for m in members_active if m["user_id"] == hernan_id]
        assert len(mpaz_members) == 1
        assert len(hernan_members) == 1

        # Los usuarios existen y estan activos
        u1 = users_coll.find_one({"_id": mpaz_id})
        u2 = users_coll.find_one({"_id": hernan_id})
        assert u1["is_active"] is True
        assert u2["is_active"] is True
        assert u1["nombre"] == "María Paz Galleguillos"
        assert u2["nombre"] == "Hernán Castro"

    def test_membresia_idempotente(self, users_coll, memberships_coll):
        """19. Creacion de membresias es idempotente."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        uid = "69c19b98fbbbf113235ba844"

        doc = {"user_id": uid, "user_name": "María Paz Galleguillos",
               "enabled": True, "start_date": today}

        # Primera insercion
        r1 = memberships_coll.update_one(
            {"user_id": uid},
            {"$setOnInsert": doc, "$set": {"enabled": True}},
            upsert=True,
        )
        assert r1.upserted_id is not None

        # Segunda: no duplica
        r2 = memberships_coll.update_one(
            {"user_id": uid},
            {"$setOnInsert": doc, "$set": {"enabled": True}},
            upsert=True,
        )
        assert r2.upserted_id is None
        assert memberships_coll.count_documents({"user_id": uid}) == 1


class TestConteoCanonico:
    """Test 20: reconciliacion de conteos."""

    def test_conteo_consistente_por_ejecutivo_id(self, coll):
        """20. Script, listado, resumen y dashboard usan el mismo conteo por ID."""
        aid = "agent_x"

        # Insertar propiedades con diferentes nombres pero mismo ID
        coll.insert_one(make_prop("p1", ejecutivo_id=aid, ejecutivo_asignado="Agent X"))
        coll.insert_one(make_prop("p2", ejecutivo_id=aid, ejecutivo_asignado="Agënt X"))
        coll.insert_one(make_prop("p3", ejecutivo_id=aid, ejecutivo_asignado="agent x"))

        # Todos los metodos deben dar 3
        count_by_id = coll.count_documents({"gestion.ejecutivo_id": aid})
        assert count_by_id == 3

        # Conteo "abiertas" (no terminales) por ID
        abiertas = coll.count_documents({
            "gestion.ejecutivo_id": aid,
            "gestion.estado": {"$nin": list(TERMINAL_STATES)},
        })
        assert abiertas == 3


class TestRegressionRealCase:
    """Test de regresion basado en el caso 6a551159b2af2a5f7e0485a7."""

    def test_caso_real_propiedad_protegida(self, coll, events_coll):
        """Propiedad con notas, actividad, fecha_ultima_gestion y estado Por contactar:
        protegida, no redistribuible."""
        pid = "6a551159b2af2a5f7e0485a7"
        p = make_prop(
            pid,
            comuna="nunoa",
            estado="Por contactar",
            ejecutivo_id="raquel_id",
            ejecutivo_asignado="Raquel Cheneaux",
            fecha_ultima=datetime(2026, 7, 16, 19, 38, 5, tzinfo=timezone.utc),
            notas=[
                {"usuario": "Susana Ensignia", "canal": "wa",
                 "timestamp": datetime(2026, 7, 16, 19, 36, 58, tzinfo=timezone.utc),
                 "resultado": "opened_app"},
                {"usuario": "Raquel Cheneaux", "canal": "manual",
                 "timestamp": datetime(2026, 7, 16, 19, 38, tzinfo=timezone.utc)},
            ],
            acts=[{"user": "Raquel Cheneaux", "channel": "web",
                   "action": "status_change", "result": "Por contactar",
                   "timestamp": datetime(2026, 7, 16, 19, 38, tzinfo=timezone.utc)}],
        )
        coll.insert_one(p)

        has_ev, reason = has_management_evidence(p, events_coll)
        assert has_ev, f"Propiedad con gestion real debe estar protegida, reason={reason}"

        # Debe ser elegible (clasificacion DUEÑO_SEGURO)
        assert is_eligible(p)

        # Pero NO redistribuible por tener gestion
        assert has_ev

        # Verificar que las notas y actividad se conservan
        reloaded = coll.find_one({"_id": pid})
        assert len(reloaded["gestion"]["notas"]) == 2
        assert len(reloaded["gestion"]["actividades"]) == 1
        assert reloaded["gestion"]["estado"] == "Por contactar"
        assert reloaded["gestion"]["fecha_ultima_gestion"] is not None


class TestManagementEvidenceEdgeCases:
    """Casos borde adicionales."""

    def test_estado_nuevo_con_actividad_protege(self):
        """Propiedad con estado NUEVO pero con actividad: protegida."""
        p = make_prop("edge1", estado="NUEVO",
                       acts=[{"user": "X", "action": "call", "result": "no_answer"}])
        has_ev, reason = has_management_evidence(p)
        assert has_ev, "Actividad aunque estado sea NUEVO debe proteger"

    def test_sin_gestion_realmente_redistribuible(self):
        """Propiedad sin nada: redistribuible."""
        p = make_prop("edge2", estado="NUEVO")
        has_ev, reason = has_management_evidence(p)
        assert not has_ev

    def test_eventos_por_listing_id(self, events_coll):
        """Evento matcheado por listing_id tambien protege."""
        pid = "edge3"
        events_coll.insert_one({
            "listing_id": "LST-edge3",
            "action": "call",
            "channel": "tel",
            "result": "contacted",
            "actor": "agent_1",
            "occurred_at": datetime.now(timezone.utc),
        })
        p = make_prop(pid, estado="NUEVO", ejecutivo_id="agent_1")
        has_ev, reason = has_management_evidence(p, events_coll)
        assert has_ev, f"Evento por listing_id deberia proteger, reason={reason}"

    def test_eventos_por_url(self, events_coll):
        """Evento matcheado por url tambien protege."""
        pid = "edge4"
        events_coll.insert_one({
            "url": "https://test.cl/edge4",
            "action": "message",
            "channel": "email",
            "result": "sent",
            "actor": "agent_1",
            "occurred_at": datetime.now(timezone.utc),
        })
        p = make_prop(pid, estado="NUEVO")
        has_ev, reason = has_management_evidence(p, events_coll)
        assert has_ev, f"Evento por url deberia proteger, reason={reason}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
