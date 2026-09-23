from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[1] / "scrapers" / "scraper_toctoc"))

from executive_config import resolve_scraping_config
import pipeline_entrypoint


class _Cursor(list):
    pass


class _Collection:
    def __init__(self, rows):
        self.rows = list(rows)

    def find(self, query=None, projection=None):
        query = query or {}
        rows = [row for row in self.rows if all(row.get(k) == v for k, v in query.items())]
        return _Cursor(rows)


class _DB:
    def __init__(self, users, memberships=None, defaults=None):
        self.collections = {
            "usuarios": _Collection(users),
            "captacion_team_memberships": _Collection(memberships or []),
            "toctoc_scraping_defaults": _Collection(defaults or []),
        }

    def __getitem__(self, key):
        return self.collections[key]

    def list_collection_names(self):
        return list(self.collections)


def _user(**extra):
    result = {
        "_id": "user-susana",
        "nombre": "Susana Ensignia",
        "rol": "agente",
        "is_active": True,
        "comunas_interes_norm": ["nunoa", "providencia"],
    }
    result.update(extra)
    return result


def test_resolver_uses_preferences_and_existing_system_defaults_with_sources():
    resolved = resolve_scraping_config(_DB([_user()]), "Susana Ensignia")

    assert resolved["found"] is True
    assert resolved["config_complete"] is True
    assert resolved["values"] == {
        "communes": ["nunoa", "providencia"],
        "operations": ["venta", "arriendo"],
        "property_types": ["departamento"],
        "min_price_clp": 0,
        "max_price_clp": None,
    }
    assert resolved["sources"]["communes"] == "CRM_EXECUTIVE_PREFERENCES"
    assert resolved["sources"]["operations"].startswith("scripts/run_toctoc_team_preferences.py")


def test_executive_override_precedes_team_and_system_defaults():
    db = _DB(
        [_user(toctoc_scraping_config={"operations": ["venta"], "property_types": ["casa"]})],
        memberships=[{"user_id": "user-susana", "team_id": "team-1"}],
        defaults=[{"team_id": "team-1", "scraping_config": {"operations": ["arriendo"], "min_price_clp": 50}}],
    )
    resolved = resolve_scraping_config(db, "Susana Ensignia")

    assert resolved["values"]["operations"] == ["venta"]
    assert resolved["values"]["property_types"] == ["casa"]
    assert resolved["values"]["min_price_clp"] == 50
    assert resolved["sources"]["operations"] == "EXECUTIVE_OVERRIDE"
    assert resolved["sources"]["min_price_clp"] == "TEAM_OFFICE_DEFAULT"


def test_missing_required_policy_is_marked_incomplete_not_guessed():
    resolved = resolve_scraping_config(
        _DB([_user()]),
        "Susana Ensignia",
        system_defaults={"operations": ["venta"], "min_price_clp": 0, "max_price_clp": None},
    )

    assert resolved["config_complete"] is False
    assert "property_types" in resolved["missing_fields"]


def test_executive_entrypoint_can_discover_without_manual_candidate_file(monkeypatch):
    resolved = resolve_scraping_config(_DB([_user()]), "Susana Ensignia")
    fake_discovery = {
        "run_id": "run-susana",
        "status": "SUCCESS",
        "records": [{"listing_id": "123", "url": "https://www.toctoc.com/property/123"}],
        "queries": [],
        "errors": [],
        "unique_count": 1,
    }
    monkeypatch.setattr(pipeline_entrypoint, "resolve_scraping_config", lambda db, executive: resolved)
    monkeypatch.setattr(pipeline_entrypoint, "discover_executive_scope", lambda *a, **k: fake_discovery)

    result = pipeline_entrypoint.run_toctoc_pipeline_for_executive(
        "Susana Ensignia", db=object(), run_id="run-susana", discovery_only=True,
    )

    assert result["run_status"] == "SUCCESS"
    assert result["discovered_count"] == 1
    assert result["processed_count"] == 0
    assert result["assigned_count"] == 0


def test_discovery_uses_region_map_and_continues_after_individual_query_failure(monkeypatch):
    calls = []

    class _ProxyManager:
        @classmethod
        def from_env(cls):
            return cls()

        def has_proxies(self):
            return False

    def fake_discover(**kwargs):
        calls.append((kwargs["comuna"], kwargs["region"], kwargs["operacion"]))
        if kwargs["comuna"] == "talca":
            raise TimeoutError("one territorial query timed out")
        return {
            "records": [{"listing_id": "same-1", "url": "https://www.toctoc.com/p/same-1"}],
            "report": {"pagination_working": True, "expected_results": 1},
        }

    discovery = SimpleNamespace(discover_listing_urls=fake_discover)
    schema = SimpleNamespace(COMUNA_TO_REGION={"ñuñoa": "Metropolitana", "Talca": "Maule"})
    monkeypatch.setattr(pipeline_entrypoint, "_discovery_module", lambda: discovery)
    monkeypatch.setattr(pipeline_entrypoint, "_load_scraper_module", lambda name: schema if name == "crm_schema" else SimpleNamespace(ProxyManager=_ProxyManager))

    result = pipeline_entrypoint.discover_executive_scope(
        {
            "values": {
                "communes": ["nunoa", "talca"],
                "operations": ["venta"],
                "property_types": ["departamento"],
            }
        },
        run_id="region-test",
        proxy_mode="auto",
    )

    assert calls == [("nunoa", "metropolitana", "venta"), ("talca", "maule", "venta")]
    assert result["unique_count"] == 1
    assert result["status"] == "COMPLETED_WITH_ANOMALIES"
    assert len(result["errors"]) == 1


def test_scraper_import_context_keeps_its_config_module_isolated():
    module = pipeline_entrypoint._discovery_module()
    assert callable(module.discover_listing_urls)
    assert hasattr(sys.modules["toctoc_runtime_config"], "AppConfig")
