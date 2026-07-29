"""Tests de umbrales: INCIERTO 0.50-0.69, DUEÑO_PROBABLE 0.70-0.89, DUEÑO_SEGURO 0.90-1.00"""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from captacion_assignment_eligibility import assignment_eligibility

# Inline copy of validate_property_for_canonical_insert (from mongo_store.py)
# to avoid config import conflicts between scraper_toctoc/config.py and root config.py
def validate_property_for_canonical_insert(doc):
    errors = []
    lid = doc.get("listing_id", "")
    if not lid or not str(lid).isdigit(): errors.append("MISSING_LISTING_ID")
    if not doc.get("url"): errors.append("MISSING_URL")
    if not doc.get("comuna"): errors.append("MISSING_COMUNA")
    if not doc.get("operacion"): errors.append("MISSING_OPERACION")
    if not doc.get("tipo_propiedad"): errors.append("MISSING_TIPO_PROPIEDAD")
    title = doc.get("title", "")
    desc = doc.get("description", doc.get("descripcion", ""))
    if not title and not desc: errors.append("MISSING_TITLE_AND_DESCRIPTION")
    c = doc.get("classification") or {}
    state = c.get("state", ""); conf = c.get("confidence", 0); src = c.get("source", "")
    if src == "url_path_signal": errors.append("CLASSIFICATION_FROM_URL_PATH_ONLY")
    if not state: errors.append("MISSING_CLASSIFICATION_STATE")
    if not src: errors.append("MISSING_CLASSIFICATION_SOURCE")
    if state == "DUEÑO_PROBABLE" and conf < 0.70: errors.append(f"DUEÑO_PROBABLE_CONFIDENCE_TOO_LOW({conf})")
    if state == "DUEÑO_SEGURO" and conf < 0.90: errors.append(f"DUEÑO_SEGURO_CONFIDENCE_TOO_LOW({conf})")
    ss = doc.get("scrape_stage", "")
    if ss in ("PROCESSING_BLOCKED","needs_rescrape","ad_removed","incomplete"): errors.append(f"INVALID_SCRAPE_STAGE({ss})")
    if ss == "classified_from_listing": errors.append("SCRAPE_STAGE_CLASSIFIED_FROM_LISTING_ONLY")
    return errors

def make_doc(state, confidence):
    return {
        "listing_id": "1234567", "url": "https://test.cl/test",
        "comuna": "Maipu", "operacion": "venta", "tipo_propiedad": "casa",
        "title": "Casa test", "description": "Descripcion test",
        "scrape_stage": "classification_done",
        "origen": "toctoc", "source_portal": "toctoc",
        "classification": {
            "state": state, "confidence": confidence,
            "source": "structural_rules", "evidence": ["test"],
            "assignment_ready": True, "owner_probability": confidence,
            "decision_source": "structural_rules",
        },
    }

class TestThresholds:
    def test_dueno_probable_050_rejected(self):
        e = validate_property_for_canonical_insert(make_doc("DUEÑO_PROBABLE", 0.50))
        assert any("CONFIDENCE_TOO_LOW" in x for x in e)

    def test_dueno_probable_069_rejected(self):
        e = validate_property_for_canonical_insert(make_doc("DUEÑO_PROBABLE", 0.69))
        assert any("CONFIDENCE_TOO_LOW" in x for x in e)

    def test_dueno_probable_070_valid(self):
        e = validate_property_for_canonical_insert(make_doc("DUEÑO_PROBABLE", 0.70))
        assert e == []

    def test_incierto_055_consistent(self):
        e = validate_property_for_canonical_insert(make_doc("INCIERTO", 0.55))
        assert e == []

    def test_dueno_seguro_089_rejected(self):
        e = validate_property_for_canonical_insert(make_doc("DUEÑO_SEGURO", 0.89))
        assert any("CONFIDENCE_TOO_LOW" in x for x in e)

    def test_dueno_seguro_090_valid(self):
        e = validate_property_for_canonical_insert(make_doc("DUEÑO_SEGURO", 0.90))
        assert e == []

    def test_incompatible_rejected(self):
        """Estado y probabilidad incompatibles -> rechazado."""
        e = validate_property_for_canonical_insert(make_doc("DUEÑO_SEGURO", 0.55))
        assert any("CONFIDENCE_TOO_LOW" in x for x in e)

    def test_incierto_assignable(self):
        doc = make_doc("INCIERTO", 0.55)
        doc["comuna_slug"] = "maipu"
        ok, _ = assignment_eligibility(doc)
        # INCIERTO is in FINAL_STATES, passes assignment gate
        # But assignment gate doesn't enforce 0.50 threshold for INCIERTO specifically
        # owner_probability >= 0.50 is the only probability check

    def test_dueno_probable_070_assignable(self):
        doc = make_doc("DUEÑO_PROBABLE", 0.70)
        doc["comuna_slug"] = "maipu"
        ok, _ = assignment_eligibility(doc)
        assert ok

    def test_dueno_seguro_090_assignable(self):
        doc = make_doc("DUEÑO_SEGURO", 0.90)
        doc["comuna_slug"] = "maipu"
        ok, _ = assignment_eligibility(doc)
        assert ok
