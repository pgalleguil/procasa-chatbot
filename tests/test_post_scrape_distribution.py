"""Tests: distribucion post-scrape (reemplaza el loop horario)."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ===== webhook: el loop horario ya no existe =====

def test_webhook_has_no_hourly_distribution_loop():
    src = (ROOT / "webhook.py").read_text(encoding="utf-8")
    assert "captacion_distribution_loop" not in src
    # El SLA semanal sí debe seguir existiendo
    assert "captacion_sla_release_loop" in src


def test_webhook_status_marks_distributor_post_scrape():
    src = (ROOT / "webhook.py").read_text(encoding="utf-8")
    assert '"status": "post_scrape_trigger"' in src


# ===== scripts/run_distribution_after_scrape.py =====

def test_post_scrape_script_calls_distribute():
    script = ROOT / "scripts" / "run_distribution_after_scrape.py"
    assert script.exists()
    src = script.read_text(encoding="utf-8")
    assert "distribute_sourced_leads" in src


# ===== disparos en los scrapers =====

def test_run_toctoc_triggers_distribution():
    src = (ROOT / "scraper_toctoc" / "run_toctoc.py").read_text(encoding="utf-8")
    assert "_run_post_scrape_distribution" in src
    assert "write_db and not args.dry_run" in src


def test_run_territorial_triggers_distribution():
    src = (ROOT / "run_territorial_expansion.py").read_text(encoding="utf-8")
    assert "run_distribution_after_scrape.py" in src
    assert "stats.get(\"persisted\", 0) > 0" in src


def test_run_toctoc_incremental_triggers_distribution():
    src = (ROOT / "scraper_toctoc" / "run_toctoc_incremental.py").read_text(encoding="utf-8")
    assert "_run_post_scrape_distribution" in src
    assert "total_insert > 0" in src


def test_yapo_pipeline_triggers_distribution():
    src = (ROOT / "scraper_yapo" / "scraping_yapo_proxys_yapo.py").read_text(encoding="utf-8")
    assert "_run_post_scrape_distribution" in src
    assert "new_inserted > 0" in src


def test_yapo_owner_hunt_triggers_distribution():
    src = (ROOT / "scraper_yapo" / "run_owner_hunt.py").read_text(encoding="utf-8")
    assert "_run_post_scrape_distribution" in src
    assert "write_db and not args.dry_run and processed" in src


# ===== el script standalone invoca distribute_sourced_leads =====

def test_standalone_script_executes_distribution(tmp_path, monkeypatch):
    import runpy
    from types import ModuleType

    fake = ModuleType("api_captacion")
    calls = {"n": 0}

    def _fake_distribute():
        calls["n"] += 1
        return 5

    fake.distribute_sourced_leads = _fake_distribute
    sys.modules["api_captacion"] = fake

    script = ROOT / "scripts" / "run_distribution_after_scrape.py"
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(script), run_name="__main__")
    assert exc.value.code == 0
    assert calls["n"] == 1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
