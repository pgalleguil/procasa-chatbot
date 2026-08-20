from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_prop360_historical_universe.py"
WEBHOOK = ROOT / "webhook.py"


def test_audit_uses_dedicated_report_collection_only():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'REPORT_COLLECTION = "prop360_audit_reports"' in source
    assert 'universo_cartera_prop360' in source
    assert 'update_one({"_id": REPORT_ID' in source
    assert 'update_one({"codigo"' not in source
    assert "insert_one" not in source
    assert "insert_many" not in source
    assert "delete_many" not in source


def test_audit_lock_and_completed_state_are_idempotent():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"status": {"$nin": ["running", "completed"]}' in source
    assert '"status": "completed"' in source
    assert '"status": "failed"' in source
    assert "MAX_DETAIL = 30" in source


def test_audit_reuses_production_client_and_listing_parameters():
    source = SCRIPT.read_text(encoding="utf-8")
    for token in (
        "Prop360Client",
        "client.login()",
        '"ac": "listadoPropiedades"',
        '"op": 2',
        '"or": 1',
        '"od": 2',
        '"vi": 2',
        '"ca": "10,1,2,3,4,5,6,7,8,9"',
    ):
        assert token in source


def test_report_does_not_persist_sensitive_payloads():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"inactive_sample"' in source
    assert '"listing_fields"' in source
    assert '"detail_results"' not in source
    assert '"html"' not in source
    assert '"password"' not in source.split('report = {', 1)[1].split('reports.update_one', 1)[0]


def test_internal_route_only_reads_sanitized_report():
    source = WEBHOOK.read_text(encoding="utf-8")
    route_start = source.index('@app.get("/internal-review/prop360-historical-audit")')
    route = source[route_start: source.index("\n\ndef _public_executive_overview", route_start)]
    assert "find_one" in route
    assert "prop360_audit_reports" in route
    assert "no-store" in route
    assert "noindex, nofollow" in route
    assert "run_prop360_historical_audit" not in route
