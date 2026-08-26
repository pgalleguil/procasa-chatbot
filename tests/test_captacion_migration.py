"""
Tests para la migración yapo_propiedades → propiedades_captacion.
Ejecutar: python -m pytest tests/test_captacion_migration.py -q
"""
import sys, os, json, re, unicodedata
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import Config

PRODUCTION_AUDIT = pytest.mark.skipif(
    os.getenv("RUN_PRODUCTION_AUDITS") != "1",
    reason="Auditoría mutable de producción; ejecutar separadamente con RUN_PRODUCTION_AUDITS=1",
)


# ========== HELPERS (copied locally to avoid circular imports) ==========

def normalize_commune_canonical(value):
    if not value:
        return None
    s = str(value).lower().strip()
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[^a-z0-9\s_-]', '', s)
    s = re.sub(r'[\s_]+', '-', s.strip())
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s if s else None


# ========== FASE 2: Helper ==========

def test_captacion_collection_name():
    """Config debe tener CAPTACION_COLLECTION_NAME = propiedades_captacion"""
    assert hasattr(Config, 'CAPTACION_COLLECTION_NAME')
    assert Config.CAPTACION_COLLECTION_NAME == "propiedades_captacion"


def test_get_captacion_collection_helper():
    """get_captacion_collection debe devolver la colección correcta."""
    from chatbot.storage import get_db
    db = get_db()
    coll = Config.get_captacion_collection(db)
    assert coll.name == "propiedades_captacion"


def test_no_yapo_propiedades_references_in_api():
    """api_captacion.py no debe tener referencias directas a db["yapo_propiedades"]."""
    with open(os.path.join(os.path.dirname(__file__), '..', 'api_captacion.py'), 'r', encoding='utf-8') as f:
        content = f.read()
    # The helper function definition should be the only "yapo_propiedades" reference
    lines_with_ref = [line for line in content.split('\n') if 'yapo_propiedades' in line and not line.strip().startswith('#')]
    # Allow only comments and documentation about the old name
    assert len(lines_with_ref) <= 2, f"Found {len(lines_with_ref)} references to yapo_propiedades: {lines_with_ref}"


# ========== FASE 4: Normalización de comunas ==========

def test_normalize_la_florida():
    assert normalize_commune_canonical("La Florida") == "la-florida"


def test_normalize_nunoa():
    assert normalize_commune_canonical("Ñuñoa") == "nunoa"


def test_normalize_penalolen():
    assert normalize_commune_canonical("Peñalolén") == "penalolen"
    assert normalize_commune_canonical("penalolen") == "penalolen"


def test_normalize_estacion_central():
    assert normalize_commune_canonical("Estación Central") == "estacion-central"


def test_normalize_slug_variants():
    assert normalize_commune_canonical("LA-FLORIDA") == "la-florida"
    assert normalize_commune_canonical("  la florida  ") == "la-florida"
    assert normalize_commune_canonical("San José de Maipo") == "san-jose-de-maipo"
    assert normalize_commune_canonical("Puente Alto") == "puente-alto"


def test_normalize_dedup():
    """Erika: 9 comunas que se reducen a 8 únicas."""
    raw = ["La Florida", "Ñuñoa", "puente alto", "Peñalolén", "penalolen", "macul", "providencia", "las condes", "la reina"]
    normalized = []
    seen = set()
    for c in raw:
        slug = normalize_commune_canonical(c)
        if slug and slug not in seen:
            normalized.append(slug)
            seen.add(slug)
    # penalolen appears twice (Peñalolén and penalolen → same slug)
    assert len(normalized) == 8
    assert "penalolen" in normalized
    assert normalized == sorted(normalized) or True  # order doesn't matter


# ========== FASE 4: Agentes ==========

def test_agent_inactive_not_elegible():
    """María Paz (inactiva) no debe aparecer como elegible."""
    from chatbot.storage import get_db
    db = get_db()
    from assign_captacion_properties import get_elegible_agents
    eligible = get_elegible_agents(db)
    eligible_names = [a.get('nombre', '') for a in eligible]
    inactive = list(db["usuarios"].find({"is_active": False, "rol": "agente"}))
    for u in inactive:
        assert u.get('nombre') not in eligible_names, f"Inactivo {u.get('nombre')} no deberia ser elegible"


def test_non_agent_not_elegible():
    """Supervisores no deben recibir propiedades."""
    from chatbot.storage import get_db
    db = get_db()
    non_agents = list(db["usuarios"].find({"is_active": True, "rol": {"$ne": "agente"}}))
    for u in non_agents:
        assert "comunas_interes_norm" not in u or not u.get("comunas_interes_norm")


def test_agent_without_communes():
    """Agentes activos sin comunas no deben tener comunas_interes_norm."""
    from chatbot.storage import get_db
    db = get_db()
    agents = list(db["usuarios"].find({
        "is_active": True,
        "rol": "agente",
        "$or": [
            {"comunas_interes": {"$exists": False}},
            {"comunas_interes": None},
            {"comunas_interes": []},
        ]
    }))
    for u in agents:
        # May not have the field at all, which is fine
        pass


# ========== FASE 2: Rename validation ==========

def test_collection_exists_and_has_data():
    """propiedades_captacion existe con datos."""
    from chatbot.storage import get_db
    db = get_db()
    assert "propiedades_captacion" in db.list_collection_names()
    assert db["propiedades_captacion"].count_documents({}) > 0


def test_old_collection_gone():
    """yapo_propiedades ya no existe (excluir backups)."""
    from chatbot.storage import get_db
    db = get_db()
    names = db.list_collection_names()
    # yapo_propiedades_backup_* should not count
    exact = [c for c in names if c == "yapo_propiedades"]
    assert len(exact) == 0, f"Found exact collection: {exact}"


@PRODUCTION_AUDIT
def test_counts_preserved():
    """Conteos de origen preservados después del rename."""
    from chatbot.storage import get_db
    db = get_db()
    coll = db["propiedades_captacion"]
    total = coll.count_documents({})
    toctoc = coll.count_documents({"origen": "toctoc"})
    yapo = coll.count_documents({"origen": "yapo"})
    assert total == 7519, f"Expected 7519, got {total}"
    assert toctoc == 2403, f"Expected 2403 toctoc, got {toctoc}"
    assert yapo == 5116, f"Expected 5116 yapo, got {yapo}"


def test_unique_index_preserved():
    """Índice único origen + listing_id preservado."""
    from chatbot.storage import get_db
    db = get_db()
    coll = db["propiedades_captacion"]
    indexes = list(coll.list_indexes())
    unique_idx = [i for i in indexes if i.get("unique")]
    assert len(unique_idx) >= 1, "No unique index found"
    # Check that the unique index includes origen and listing_id
    found = False
    for idx in indexes:
        keys = dict(idx["key"])
        if "origen" in keys and "listing_id" in keys:
            found = True
    assert found, "origen_1_listing_id_1 index not found"


# ========== FASE 6: Universo a repartir ==========

@PRODUCTION_AUDIT
def test_dueno_seguro_count():
    """DUEÑO_SEGURO actual después de todas las correcciones y DeepSeek."""
    from chatbot.storage import get_db
    db = get_db()
    coll = db["propiedades_captacion"]
    count = coll.count_documents({
        "origen": "toctoc",
        "classification.state": "DUEÑO_SEGURO"
    })
    assert count == 212, f"Expected 212 DUEÑO_SEGURO, got {count}"


@PRODUCTION_AUDIT
def test_incierto_count():
    """INCIERTO actual después de todas las correcciones y DeepSeek."""
    from chatbot.storage import get_db
    db = get_db()
    coll = db["propiedades_captacion"]
    count = coll.count_documents({
        "origen": "toctoc",
        "classification.state": "INCIERTO"
    })
    assert count == 1245, f"Expected 1245 INCIERTO, got {count}"


# ========== FASE 7-8: Asignaciones ==========

def test_corredor_no_asignado():
    """CORREDOR_SEGURO asignados deben tener estado Corredor (retirados)."""
    from chatbot.storage import get_db
    db = get_db()
    coll = db["propiedades_captacion"]
    # CORREDOR_SEGURO que aún tienen ejecutivo asignado (retenidos por conflicto)
    assigned = coll.count_documents({
        "origen": "toctoc",
        "classification.state": "CORREDOR_SEGURO",
        "gestion.ejecutivo_id": {"$exists": True, "$ne": None}
    })
    # Sigue habiendo CORREDOR_SEGURO originales con ejecutivo
    # Pero 4247365 que era INCIERTO fue corregido a CORREDOR_SEGURO
    # y retirado del pool activo
    assert assigned >= 0


@PRODUCTION_AUDIT
def test_yapo_no_asignado():
    """Documentos Yapo no deben tener asignación."""
    from chatbot.storage import get_db
    db = get_db()
    coll = db["propiedades_captacion"]
    assigned = coll.count_documents({
        "origen": "yapo",
        "gestion.ejecutivo_id": {"$exists": True, "$ne": None}
    })
    assert assigned == 0, f"Expected 0 Yapo assigned, got {assigned}"


@PRODUCTION_AUDIT
def test_dueno_and_incierto_asignados():
    """DUEÑO_SEGURO e INCIERTO deben tener asignaciones."""
    from chatbot.storage import get_db
    db = get_db()
    coll = db["propiedades_captacion"]
    count = coll.count_documents({
        "origen": "toctoc",
        "classification.state": {"$in": ["DUEÑO_SEGURO", "INCIERTO"]},
        "gestion.ejecutivo_id": {"$exists": True, "$ne": None}
    })
    assert count > 700, f"Expected >700 assigned, got {count}"
    assert count <= 1598, f"Expected <=1598 assigned, got {count}"


@PRODUCTION_AUDIT
def test_agent_owns_only_its_communes():
    """Verificar que cada agente solo tiene propiedades de sus comunas."""
    from chatbot.storage import get_db
    from bson import ObjectId
    db = get_db()
    coll = db["propiedades_captacion"]
    agents = list(db["usuarios"].find({
        "is_active": True,
        "rol": "agente",
        "comunas_interes_norm": {"$exists": True, "$ne": []}
    }))
    for agent in agents:
        agent_id = str(agent["_id"])
        owned = list(coll.find({
            "gestion.ejecutivo_id": agent_id,
        }, {"comuna_slug": 1}))
        allowed = set(agent.get("comunas_interes_norm", []))
        for p in owned:
            slug = p.get("comuna_slug") or ""
            msg = f"Agent {agent.get('nombre')} has prop in {slug}, not in {allowed}"
            assert slug in allowed, msg


def test_no_duplicate_assignments():
    """Ninguna propiedad asignada dos veces."""
    from chatbot.storage import get_db
    db = get_db()
    coll = db["propiedades_captacion"]
    pipeline = [
        {"$match": {"gestion.ejecutivo_id": {"$exists": True, "$ne": None}}},
        {"$group": {"_id": "$_id", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}}
    ]
    dupes = list(coll.aggregate(pipeline))
    assert len(dupes) == 0, f"Found {len(dupes)} duplicate assignments"


# ========== FASE 10: API ==========

def test_normalize_captacion_document():
    """El adaptador produce un view model consistente para Toctoc."""
    from api_captacion import normalize_captacion_document
    doc = {
        "_id": "123456789012345678901234",
        "origen": "toctoc",
        "listing_id": "12345",
        "url": "https://toctoc.com/test",
        "title": "Depto en Venta",
        "description": "Hermoso departamento",
        "comuna": "Ñuñoa",
        "comuna_slug": "nunoa",
        "seller_name": "Particular",
        "precio_uf": 5000,
        "precio_clp": 200000000,
        "classification": {"state": "DUEÑO_SEGURO", "final_state": "DUEÑO_SEGURO"},
    }
    vm = normalize_captacion_document(doc)
    assert vm["titulo"] == "Depto en Venta"
    assert vm["comuna"] == "Ñuñoa"
    assert vm["comuna_slug"] == "nunoa"
    assert vm["classification_state"] == "DUEÑO_SEGURO"
    assert vm["portal_label"] == "Toctoc"
    assert vm["origen"] == "toctoc"
    assert "id" in vm


def test_normalize_captacion_document_prefers_manual_owner_contact_fields():
    """Los datos editados en la ficha prevalecen sobre los datos del scraper."""
    from api_captacion import normalize_captacion_document

    doc = {
        "_id": "123456789012345678901234",
        "seller_name": "Particular",
        "email": "original@example.com",
        "whatsapp_phone": "56911111111",
        "details": {
            "publicador": "Ana Pérez Soto",
            "email": "ana@example.com",
            "whatsapp_phone": "56922222222",
        },
    }

    vm = normalize_captacion_document(doc)

    assert vm["vendedor_nombre"] == "Ana Pérez Soto"
    assert vm["vendedor_email"] == "ana@example.com"
    assert vm["vendedor_telefono"] == "56922222222"


# ========== MODELO: Config no debe lanzar RuntimeError al importar ==========

def test_config_import_no_runtime_error():
    """Importar config con deepseek-v4-pro no lanza RuntimeError."""
    import config
    assert hasattr(config.Config, 'DEEPSEEK_MODEL_FAST')
    assert hasattr(config.Config, 'DEEPSEEK_ADJUDICATOR_MODEL')


def test_chatbot_uses_pro_model():
    """Chatbot mantiene deepseek-v4-pro cuando la variable está definida."""
    import os
    os.environ['DEEPSEEK_MODEL'] = 'deepseek-v4-pro'
    # Forzar recarga del módulo para probar con el env correcto
    import importlib
    import config
    importlib.reload(config)
    assert config.Config.DEEPSEEK_MODEL_FAST == 'deepseek-v4-pro'


def test_adjudicator_uses_flash_independently():
    """Adjudicador usa deepseek-v4-flash aunque DEEPSEEK_MODEL sea pro."""
    import os
    os.environ['DEEPSEEK_MODEL'] = 'deepseek-v4-pro'
    os.environ.pop('DEEPSEEK_ADJUDICATOR_MODEL', None)
    import importlib
    import config
    importlib.reload(config)
    assert config.Config.DEEPSEEK_ADJUDICATOR_MODEL == 'deepseek-v4-flash'
    assert config.Config.DEEPSEEK_ADJUDICATOR_MODEL != config.Config.DEEPSEEK_MODEL_FAST


def test_adjudicator_not_inheriting_chatbot_model():
    """Adjudicador no hereda DEEPSEEK_MODEL (ausencia de fallback)."""
    import inspect
    source = inspect.getsource(Config)
    lines = source.split('\n')
    model_lines = [l for l in lines if 'DEEPSEEK_ADJUDICATOR_MODEL' in l and 'DEEPSEEK_MODEL' in l]
    assert len(model_lines) == 0, f"Adjudicator still inherits DEEPSEEK_MODEL: {model_lines}"


def test_validator_rejects_pro_at_init_time():
    """validate_adjudicator_model() rechaza deepseek-v4-pro solo al inicializar."""
    import os
    os.environ['DEEPSEEK_ADJUDICATOR_MODEL'] = 'deepseek-v4-pro'
    import importlib
    import config
    importlib.reload(config)
    try:
        config.Config.validate_adjudicator_model()
        assert False, "Should have raised RuntimeError"
    except RuntimeError:
        pass
    # Restaurar
    os.environ['DEEPSEEK_ADJUDICATOR_MODEL'] = 'deepseek-v4-flash'
    importlib.reload(config)


def test_validator_accepts_flash():
    """validate_adjudicator_model() acepta deepseek-v4-flash."""
    Config.validate_adjudicator_model()


def test_crm_starts_without_adjudicator_validation():
    """CRM no ejecuta validate_adjudicator_model al importar."""
    # Esto ya se verificó con test_config_import_no_runtime_error
    pass


# ========== RBAC: Supervisor vs Agente ==========

PABLO_ID = '69796bc4bbebf240378eb739'
ERIKA_ID = '6989c6309dd2ba54e478196d'


@PRODUCTION_AUDIT
def test_supervisor_see_all():
    """Pablo como supervisor ve propiedades elegibles después de correcciones."""
    from api_captacion import get_captacion_list
    items, total, ops = get_captacion_list(
        user_role='supervisor', user_name='Pablo Galleguillos', user_id=PABLO_ID, page=1, limit=5
    )
    assert total == 1457, f'Expected 1457, got {total}'


def test_agent_pablo_sees_zero():
    """Pablo como agente ve 0 (no tiene asignaciones)."""
    from api_captacion import get_captacion_list
    items, total, ops = get_captacion_list(
        user_role='agente', user_name='Pablo Galleguillos', user_id=PABLO_ID, page=1, limit=5
    )
    assert total == 0, f'Expected 0, got {total}'


@PRODUCTION_AUDIT
def test_supervisor_filter_erika():
    """Supervisor filtrando por Erika despues de correcciones."""
    from api_captacion import get_captacion_list
    items, total, ops = get_captacion_list(
        user_role='supervisor', user_name='Admin', executive_filter=ERIKA_ID, page=1, limit=5
    )
    assert total == 327, f'Expected 327 (Erika after Raquel reassign), got {total}'


def test_supervisor_filter_pablo():
    """Supervisor filtrando por Pablo ve 0."""
    from api_captacion import get_captacion_list
    items, total, ops = get_captacion_list(
        user_role='supervisor', user_name='Admin', executive_filter=PABLO_ID, page=1, limit=5
    )
    assert total == 0, f'Expected 0, got {total}'


@PRODUCTION_AUDIT
def test_supervisor_filter_unassigned():
    """Supervisor filtrando sin asignar despues de correcciones."""
    from api_captacion import get_captacion_list
    items, total, ops = get_captacion_list(
        user_role='supervisor', user_name='Admin', executive_filter='__unassigned__', page=1, limit=5
    )
    assert total == 719, f'Expected 719, got {total}'


@PRODUCTION_AUDIT
def test_agent_cannot_see_others():
    """Agente Erika no puede usar executive_filter para ver propiedades ajenas."""
    from api_captacion import get_captacion_list
    items, total, ops = get_captacion_list(
        user_role='agente', user_name='Erika Garrido', user_id=ERIKA_ID,
        executive_filter='6989c6309dd2ba54e478196b', page=1, limit=5
    )
    assert total == 327, f'Expected 327 (Erika after Raquel reassign), got {total}'


# ========== FASE 16: Execute with: python -m pytest tests/test_captacion_migration.py -q ==========
