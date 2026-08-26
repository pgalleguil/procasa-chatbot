from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(name):
    return (ROOT / name).read_text(encoding="utf-8-sig")


def test_contract_links_exclude_deleted_documents_from_every_token_flow():
    source = _source("api_contracts.py")

    assert "def active_token_query(token: str)" in source
    assert '"status": {"$ne": "deleted"}' in source
    assert source.count("active_token_query(token)") >= 6


def test_visit_links_exclude_deleted_documents_from_every_token_flow():
    source = _source("api_visitas.py")

    assert "def active_token_query(token: str)" in source
    assert '"status": {"$ne": "deleted"}' in source
    assert source.count("active_token_query(token)") >= 6


def test_deletion_revokes_token_and_otp_credentials_in_both_modules():
    for name in ("api_contracts.py", "api_visitas.py"):
        source = _source(name)
        assert '"security.token": None' in source
        assert '"security.token_revoked_at": revoked_at' in source
        assert '"security.otp": None' in source
        assert '"action": "document_deleted"' in source
