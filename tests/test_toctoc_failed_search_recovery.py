from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[1] / "scrapers" / "scraper_toctoc"))

import pipeline_entrypoint as pipeline


def _resolved(name: str, communes: list[str]) -> dict:
    return {
        "found": True,
        "config_complete": True,
        "executive": name,
        "values": {
            "communes": communes,
            "operations": ["venta", "arriendo"],
            "property_types": ["departamento"],
            "min_price_clp": 0,
            "max_price_clp": None,
        },
    }


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_only_failed_unique_searches_are_retried_and_processed(tmp_path, monkeypatch):
    previous = "old-run"
    reports = tmp_path / "reports"
    reports.mkdir()
    resolved_by_name = {
        "A": _resolved("A", ["Providencia", "Ñuñoa"]),
        "B": _resolved("B", ["Providencia", "San Miguel"]),
    }
    monkeypatch.setattr(
        pipeline,
        "resolve_scraping_config",
        lambda db, executive: resolved_by_name[executive],
    )

    batch_ids = {
        "A": [f"{previous}-a-{index:04d}" for index in range(1, 5)],
        "B": [f"{previous}-b-{index:04d}" for index in range(1, 5)],
    }
    checkpoint_states = {
        batch_ids["A"][0]: {"stop_reason": "NEXT_NOT_FOUND", "page_reports": [{"completed": True}]},
        batch_ids["A"][1]: {"stop_reason": "NEXT_NOT_FOUND", "page_reports": [{"completed": True}]},
        batch_ids["A"][2]: {"stop_reason": "SPA_LOCATION_INPUT_NOT_FOUND", "last_completed_page": 0, "next_page": 1},
        batch_ids["A"][3]: {"stop_reason": "PLAYWRIGHT_ERROR", "last_completed_page": 0, "next_page": 1},
        batch_ids["B"][0]: {"stop_reason": "PLAYWRIGHT_ERROR", "last_completed_page": 0, "next_page": 1},
        batch_ids["B"][1]: {"stop_reason": "SPA_LOCATION_INPUT_NOT_FOUND", "last_completed_page": 0, "next_page": 1},
        batch_ids["B"][2]: {"stop_reason": "PAGINATION_DEGRADED_PAGE_DID_NOT_CHANGE", "last_completed_page": 2, "next_page": 4},
        batch_ids["B"][3]: {"stop_reason": "PLAYWRIGHT_ERROR", "last_completed_page": 0, "next_page": 1},
    }
    for batch_id, checkpoint in checkpoint_states.items():
        _write_json(reports / f"discovery_checkpoint_{batch_id}.json", checkpoint)
    _write_json(
        reports / f"discovered_{previous}-a.json",
        [
            {
                "listing_id": f"completed-{operation}",
                "url": f"https://www.toctoc.com/{operation}/departamento/metropolitana/providencia/b_completed-{operation}",
                "comuna_solicitada": "Providencia",
                "operation_requested": operation,
                "property_type_requested": "departamento",
            }
            for operation in ("venta", "arriendo")
        ],
    )
    _write_json(reports / f"discovered_{previous}-b.json", [])
    _write_json(
        reports / f"processed_{previous}-a.json",
        [{"listing_id": "recovered-nunoa-venta", "processing_status": "AD_REMOVED"}],
    )

    attempts: list[tuple[str, str, str]] = []

    def fake_discover(resolved, *, run_id, query_cache, resume_from_batch_ids, **kwargs):
        records = []
        for commune in resolved["values"]["communes"]:
            for operation in resolved["values"]["operations"]:
                key = pipeline._discovery_query_key(commune, operation, "departamento")
                cached = query_cache.get(key)
                if cached is not None:
                    records.extend(cached["records"])
                    continue
                assert key in resume_from_batch_ids
                attempts.append(key)
                record = {
                    "listing_id": f"recovered-{key[0]}-{key[1]}",
                    "url": f"https://www.toctoc.com/{operation}/departamento/metropolitana/{key[0]}/b_new",
                    "comuna_solicitada": commune,
                    "operation_requested": operation,
                    "property_type_requested": "departamento",
                }
                query_cache[key] = {"records": [record], "report": {"discovery_degraded": False}}
                records.append(record)
        return {"run_id": run_id, "status": "SUCCESS", "records": records, "queries": [], "errors": [], "unique_count": len(records)}

    monkeypatch.setattr(pipeline, "discover_executive_scope", fake_discover)
    processed_inputs: list[list[str]] = []

    def fake_process(scope, *, candidates_path, **kwargs):
        records = pipeline._load_candidates(candidates_path)
        processed_inputs.append([row["listing_id"] for row in records])
        return {"run_status": "SUCCESS", "processed_count": len(records), "records": []}

    monkeypatch.setattr(pipeline, "run_configured_toctoc_pipeline", fake_process)
    config = SimpleNamespace(reports_dir=str(reports))

    result = pipeline.recover_failed_toctoc_searches(
        ["A", "B"],
        previous_run_id_prefix=previous,
        recovery_run_id_prefix="recovery-test",
        db=object(),
        config=config,
        write_db=False,
        allow_real_ai=False,
        assignment_enabled=False,
        expected_searches=8,
    )

    assert result["searches_total"] == 8
    assert result["searches_previously_complete"] == 2
    assert result["searches_retried"] == 4
    assert result["recovered_searches"] == 4
    assert result["rediscovered_unique_listings"] == 4
    assert result["previously_processed_reused"] == 1
    assert result["new_unique_listings"] == 3
    assert len(attempts) == 4
    assert {(key[0], key[1]) for key in attempts} == {
        ("nunoa", "venta"), ("nunoa", "arriendo"),
        ("san-miguel", "venta"), ("san-miguel", "arriendo"),
    }
    assert set(processed_inputs[0]) == {
        "recovered-nunoa-venta", "recovered-nunoa-arriendo",
        "recovered-san-miguel-venta", "recovered-san-miguel-arriendo",
    } - {"recovered-nunoa-venta"}
    assert not any(item.startswith("completed-") for item in processed_inputs[0])
    assert result["distribution"]["status"] == "DISABLED"


def test_max_limited_checkpoint_is_not_treated_as_complete():
    assert pipeline._checkpoint_completed({"stop_reason": "MAX_PAGES_REACHED"}) is False
    assert pipeline._checkpoint_completed({"stop_reason": "MAX_UNIQUE_URLS_REACHED"}) is False
    assert pipeline._checkpoint_completed({"stop_reason": "NEXT_NOT_FOUND"}) is True


def test_resume_staging_preserves_original_evidence_and_rekeys_copy(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    old_batch = "old-search"
    new_batch = "new-search"
    _write_json(reports / f"discovery_progress_{old_batch}.json", [{"listing_id": "42", "batch_id": old_batch}])
    original_checkpoint = {"batch_id": old_batch, "search_url": "https://www.toctoc.com/search", "next_page": 4}
    _write_json(reports / f"discovery_checkpoint_{old_batch}.json", original_checkpoint)

    staged = pipeline._seed_resume_files(SimpleNamespace(REPORTS_DIR=reports), old_batch, new_batch)

    assert staged is True
    assert json.loads((reports / f"discovery_checkpoint_{old_batch}.json").read_text(encoding="utf-8")) == original_checkpoint
    copied_checkpoint = json.loads((reports / f"discovery_checkpoint_{new_batch}.json").read_text(encoding="utf-8"))
    copied_progress = json.loads((reports / f"discovery_progress_{new_batch}.json").read_text(encoding="utf-8"))
    assert copied_checkpoint["batch_id"] == new_batch
    assert copied_checkpoint["recovered_from_batch"] == old_batch
    assert copied_progress[0]["batch_id"] == new_batch
    assert copied_progress[0]["listing_id"] == "42"
