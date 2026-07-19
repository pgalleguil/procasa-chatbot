"""
Tests de seguridad del asignador: verifica que la query filtra correctamente.
"""
import sys, os, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.chdir(os.path.join(os.path.dirname(__file__), '..'))
from pymongo import MongoClient
from config import Config

PRODUCTION_AUDIT = pytest.mark.skipif(
    os.getenv("RUN_PRODUCTION_AUDITS") != "1",
    reason="Auditoría mutable de producción; ejecutar separadamente con RUN_PRODUCTION_AUDITS=1",
)

DUENO_STATE = 'DUE' + chr(209) + 'O_SEGURO'

def get_coll():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    return db[Config.CAPTACION_COLLECTION_NAME]

# ============ TARGET QUERY (same as assign_captacion_properties.py) ============
TARGET_QUERY = {
    "origen": "toctoc",
    "gestion.semantic_review_hold": {"$ne": True},
    "$and": [
        {
            "$or": [
                {
                    "classification.state": DUENO_STATE,
                    "classification.semantic_check.status": {"$in": ["VALID", "SKIPPED_EXPLICIT_OWNER"]}
                },
                {
                    "classification.state": "INCIERTO",
                    "classification.semantic_check.status": "VALID"
                }
            ]
        },
        {
            "$or": [
                {"gestion.ejecutivo_id": {"$exists": False}},
                {"gestion.ejecutivo_id": None},
            ]
        }
    ]
}

def test_corredor_seguro_not_in_target():
    """CORREDOR_SEGURO sin ejecutivo NO debe entrar al universo objetivo."""
    coll = get_coll()
    # Find a CORREDOR_SEGURO without ejecutivo
    sample = coll.find_one({
        "origen": "toctoc",
        "classification.state": "CORREDOR_SEGURO",
        "$or": [{"gestion.ejecutivo_id": {"$exists": False}}, {"gestion.ejecutivo_id": None}]
    })
    if sample:
        lid = sample.get('listing_id')
        # Check it's NOT in the target query
        in_target = coll.count_documents({
            **TARGET_QUERY,
            "listing_id": lid
        })
        assert in_target == 0, f"CORREDOR_SEGURO {lid} no deberia estar en target"

def test_corredor_probable_not_in_target():
    """CORREDOR_PROBABLE sin ejecutivo NO debe entrar al universo objetivo."""
    coll = get_coll()
    sample = coll.find_one({
        "origen": "toctoc",
        "classification.state": "CORREDOR_PROBABLE",
        "$or": [{"gestion.ejecutivo_id": {"$exists": False}}, {"gestion.ejecutivo_id": None}]
    })
    if sample:
        lid = sample.get('listing_id')
        in_target = coll.count_documents({
            **TARGET_QUERY,
            "listing_id": lid
        })
        assert in_target == 0, f"CORREDOR_PROBABLE {lid} no deberia estar en target"

def test_dueno_valid_in_target():
    """DUEÑO_SEGURO con semantic_check VALID SÍ debe entrar."""
    coll = get_coll()
    count = coll.count_documents({
        **TARGET_QUERY,
        "classification.state": DUENO_STATE,
        "classification.semantic_check.status": "VALID"
    })
    assert count > 0, "DUENO VALID deberia estar en target"
    assert count == coll.count_documents({
        "origen": "toctoc", "classification.state": DUENO_STATE,
        "classification.semantic_check.status": "VALID",
        "gestion.semantic_review_hold": {"$ne": True},
        "$or": [{"gestion.ejecutivo_id": {"$exists": False}}, {"gestion.ejecutivo_id": None}]
    })

def test_dueno_skipped_owner_in_target():
    """DUEÑO_SEGURO con SKIPPED_EXPLICIT_OWNER SÍ debe entrar."""
    coll = get_coll()
    count = coll.count_documents({
        **TARGET_QUERY,
        "classification.state": DUENO_STATE,
        "classification.semantic_check.status": "SKIPPED_EXPLICIT_OWNER"
    })
    assert count > 0, "DUENO SKIPPED_EXPLICIT_OWNER deberia estar en target"

def test_incierto_valid_in_target():
    """INCIERTO con semantic_check VALID SÍ debe entrar."""
    coll = get_coll()
    count = coll.count_documents({
        **TARGET_QUERY,
        "classification.state": "INCIERTO",
        "classification.semantic_check.status": "VALID"
    })
    assert count > 0, "INCIERTO VALID deberia estar en target"

def test_error_not_in_target():
    """ERROR no debe entrar al universo objetivo."""
    coll = get_coll()
    count = coll.count_documents({
        **TARGET_QUERY,
        "classification.semantic_check.status": "ERROR"
    })
    assert count == 0, "ERROR no deberia estar en target"

def test_no_description_not_in_target():
    """NO_DESCRIPTION no debe entrar al universo objetivo."""
    coll = get_coll()
    count = coll.count_documents({
        **TARGET_QUERY,
        "classification.semantic_check.status": "NO_DESCRIPTION"
    })
    assert count == 0, "NO_DESCRIPTION no deberia estar en target"

def test_hold_not_in_target():
    """Documentos con semantic_review_hold=true no deben entrar."""
    coll = get_coll()
    count = coll.count_documents({
        **TARGET_QUERY,
        "gestion.semantic_review_hold": True
    })
    assert count == 0, "HOLD no deberia estar en target"

def test_yapo_not_in_target():
    """Documentos Yapo no deben entrar (origen debe filtrarlos)."""
    coll = get_coll()
    # Yapo docs with the same state conditions
    yapo_query = {
        "origen": "yapo",
        "classification.state": {"$in": [DUENO_STATE, "INCIERTO"]}
    }
    count = coll.count_documents(yapo_query)
    if count > 0:
        in_target = coll.count_documents({
            **{k: v for k, v in TARGET_QUERY.items() if k != 'origen'},
            "origen": "yapo",
            "classification.state": {"$in": [DUENO_STATE, "INCIERTO"]}
        })
        assert in_target == 0, "Yapo no deberia estar en target del asignador"

def test_existing_ejecutivo_not_reassigned():
    """Documentos con ejecutivo existente no deben reasignarse."""
    coll = get_coll()
    with_eje = coll.count_documents({
        "origen": "toctoc",
        "gestion.ejecutivo_id": {"$exists": True, "$ne": None}
    })
    if with_eje > 0:
        in_target = coll.count_documents({
            **TARGET_QUERY,
            "gestion.ejecutivo_id": {"$exists": True, "$ne": None}
        })
        assert in_target == 0, "Documentos con ejecutivo no deberian reasignarse"

@PRODUCTION_AUDIT
def test_target_count_694():
    """Universo objetivo debe ser exactamente 694 (112 DUENO + 582 INCIERTO)."""
    coll = get_coll()
    count = coll.count_documents(TARGET_QUERY)
    assert count == 694, f"Expected 694 targets, got {count}"
    # Verify breakdown
    pipe = [
        {"$match": TARGET_QUERY},
        {"$group": {"_id": "$classification.state", "count": {"$sum": 1}}}
    ]
    for s in coll.aggregate(pipe):
        if s['_id'] == DUENO_STATE:
            assert s['count'] == 112, f"Expected 112 DUENO, got {s['count']}"
        elif s['_id'] == 'INCIERTO':
            assert s['count'] == 582, f"Expected 582 INCIERTO, got {s['count']}"
