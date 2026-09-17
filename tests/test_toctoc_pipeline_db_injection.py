from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from classification_service import classify_capture
from toctoc_pipeline import InMemoryPipelineLedger, PipelineOptions, run_toctoc_pipeline


class _FakeCollection:
    def __init__(self, documents):
        self.documents = list(documents)

    def find_one(self, query):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                return dict(document)
        return None


class _FakeMongo:
    def __init__(self, key_documents, identity_documents):
        self.collections = {
            "broker_identity_keys": _FakeCollection(key_documents),
            "broker_identities": _FakeCollection(identity_documents),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _record(code: str):
    return {
        "listing_id": code,
        "url": f"https://www.toctoc.com/test/{code}",
        "source_portal": "toctoc",
        "publicador_visible": "Ana Perez",
        "seller_type": "PARTICULAR",
        "seller_profile_id": "profile-db-1",
        "seller_client_id": "client-db-1",
        "title": "Casa en venta",
        "description": "Publicación de prueba para validar la identidad persistida.",
        "comuna": "Maipu",
        "precio_clp": 150000000,
    }


def test_pipeline_injects_db_into_identity_resolution_and_blocks_match():
    db = _FakeMongo(
        key_documents=[
            {
                "key_type": "PORTAL_PROFILE_ID",
                "key_value": "toctoc:profile-db-1",
                "identity_id": "broker:db-1",
            }
        ],
        identity_documents=[{"_id": "broker:db-1", "canonical_name": "Ana Perez"}],
    )
    report = run_toctoc_pipeline(
        [_record("db-injection")],
        db=db,
        ledger=InMemoryPipelineLedger(),
        options=PipelineOptions(test_mode=True),
    )

    assert report["run_status"] == "SUCCESS"
    assert report["assignable"] == 0
    assert report["ai"]["AI_CALLS_EXECUTED"] == 0
    assert report["qa_sample"]["BROKER"]


def test_direct_classification_keeps_injected_db_as_resolution_dependency():
    db = _FakeMongo(
        key_documents=[
            {
                "key_type": "PORTAL_CLIENT_ID",
                "key_value": "toctoc:client-db-1",
                "identity_id": "broker:db-2",
            }
        ],
        identity_documents=[{"_id": "broker:db-2", "canonical_name": "Ana Perez"}],
    )
    result = classify_capture(_record("direct-db-injection"), db=db)

    assert result["reason"] == "identity_match"
    assert result["classification"]["final"] == "BROKER_CONFIRMED"
    assert result["classification"]["assignment_ready"] is False
