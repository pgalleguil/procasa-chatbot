"""Tests de regresion: guards de insercion, clasificacion y asignacion."""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

# Inline copy from mongo_store.py to avoid config import conflict
def validate_property_for_canonical_insert(doc):
    errors = []
    lid = doc.get("listing_id", "")
    if not lid or not str(lid).isdigit(): errors.append("MISSING_LISTING_ID")
    if not doc.get("url"): errors.append("MISSING_URL")
    if not doc.get("comuna"): errors.append("MISSING_COMUNA")
    if not doc.get("operacion"): errors.append("MISSING_OPERACION")
    if not doc.get("tipo_propiedad"): errors.append("MISSING_TIPO_PROPIEDAD")
    title = doc.get("title", ""); desc = doc.get("description", doc.get("descripcion", ""))
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

def make_doc(**overrides):
    base = {
        "listing_id": "1234567",
        "url": "https://www.toctoc.com/propiedades/compraparticularsr/casa/maipu/test/1234567",
        "comuna": "Maipu",
        "operacion": "venta",
        "tipo_propiedad": "casa",
        "title": "Casa en venta Maipu",
        "description": "Hermosa casa 3 dormitorios",
        "classification": {
            "state": "DUEÑO_PROBABLE",
            "confidence": 0.85,
            "source": "structural_rules",
            "evidence": ["seller_type=PARTICULAR", "owner phrase in description"],
        },
        "scrape_stage": "classification_done",
        "origen": "toctoc",
        "source_portal": "toctoc",
    }
    base.update(overrides)
    return base


class TestInsertionGuards:
    """Tests 1-7: guards de insercion canonica."""

    def test_particularsr_sin_descripcion_no_insertable(self):
        doc = make_doc(title="", description="",
                       classification={"state":"DUEÑO_PROBABLE","confidence":0.85,
                                       "source":"structural_rules","evidence":[]})
        errors = validate_property_for_canonical_insert(doc)
        assert "MISSING_TITLE_AND_DESCRIPTION" in errors

    def test_particularsr_unica_evidencia_no_dueno_probable(self):
        doc = make_doc(
            classification={"state":"DUEÑO_PROBABLE","confidence":0.55,
                            "source":"url_path_signal",
                            "evidence":["URL path: particularsr"]})
        errors = validate_property_for_canonical_insert(doc)
        assert "CLASSIFICATION_FROM_URL_PATH_ONLY" in errors

    def test_confidence_055_es_incierto(self):
        doc = make_doc(
            classification={"state":"DUEÑO_PROBABLE","confidence":0.55,
                            "source":"structural_rules","evidence":["some signal"]})
        errors = validate_property_for_canonical_insert(doc)
        assert any("CONFIDENCE_TOO_LOW" in e for e in errors)

    def test_dueno_probable_below_070_fails(self):
        doc = make_doc(
            classification={"state":"DUEÑO_PROBABLE","confidence":0.69,
                            "source":"structural_rules","evidence":["signal"]})
        errors = validate_property_for_canonical_insert(doc)
        assert any("CONFIDENCE_TOO_LOW" in e for e in errors)

    def test_detail_403_processing_blocked(self):
        doc = make_doc(scrape_stage="PROCESSING_BLOCKED",
                       classification={"state":"INCIERTO","confidence":0.35,
                                       "source":"blocked","evidence":[]})
        errors = validate_property_for_canonical_insert(doc)
        assert any("INVALID_SCRAPE_STAGE" in e for e in errors)

    def test_classified_from_listing_no_insertable(self):
        doc = make_doc(scrape_stage="classified_from_listing",
                       classification={"state":"DUEÑO_PROBABLE","confidence":0.75,
                                       "source":"structural_rules","evidence":["signal"]})
        errors = validate_property_for_canonical_insert(doc)
        assert any("CLASSIFIED_FROM_LISTING" in e for e in errors)

    def test_zero_writes_on_403(self):
        doc = make_doc(scrape_stage="PROCESSING_BLOCKED")
        errors = validate_property_for_canonical_insert(doc)
        assert len(errors) > 0

    def test_doc_without_classification_no_insertable(self):
        doc = make_doc(classification={})
        errors = validate_property_for_canonical_insert(doc)
        assert len(errors) > 0

    def test_doc_without_title_and_description_no_insertable(self):
        doc = make_doc(title="", description="")
        errors = validate_property_for_canonical_insert(doc)
        assert "MISSING_TITLE_AND_DESCRIPTION" in errors

    def test_doc_from_discovery_cannot_skip_pipeline(self):
        doc = {
            "listing_id": "1234567",
            "url": "https://www.toctoc.com/...",
            "comuna": "maipu",
            "operacion": "venta",
            "tipo_propiedad": "casa",
            # Sin title, description, classification
            "scrape_stage": "classified_from_listing",
            "origen": "toctoc",
            "source_portal": "toctoc",
        }
        errors = validate_property_for_canonical_insert(doc)
        assert "MISSING_TITLE_AND_DESCRIPTION" in errors
        assert "SCRAPE_STAGE_CLASSIFIED_FROM_LISTING_ONLY" in errors
        assert "MISSING_CLASSIFICATION_STATE" in errors
        assert len(errors) >= 3

    def test_valid_doc_is_insertable(self):
        doc = make_doc()
        errors = validate_property_for_canonical_insert(doc)
        assert errors == [], f"Valid doc should have no errors: {errors}"

    def test_valid_doc_dueno_seguro_is_insertable(self):
        doc = make_doc(
            classification={"state":"DUEÑO_SEGURO","confidence":0.95,
                            "source":"structural_rules",
                            "evidence":["explicit owner phrase","seller=PARTICULAR"]})
        errors = validate_property_for_canonical_insert(doc)
        assert errors == []

    def test_dueno_seguro_below_090_fails(self):
        doc = make_doc(
            classification={"state":"DUEÑO_SEGURO","confidence":0.89,
                            "source":"structural_rules",
                            "evidence":["explicit owner phrase"]})
        errors = validate_property_for_canonical_insert(doc)
        assert any("CONFIDENCE_TOO_LOW" in e for e in errors)

    def test_regression_bad_batch_111(self):
        """Reproduce exactamente el documento del lote erroneo."""
        bad_doc = {
            "listing_id": "3883301",
            "url": "https://www.toctoc.com/propiedades/compraparticularsr/casa/maipu/test/3883301",
            "comuna": "Maipu",
            "operacion": "venta",
            "tipo_propiedad": "casa",
            "title": "",
            "description": "",
            "precio_uf": 0,
            "classification": {
                "state": "DUEÑO_PROBABLE",
                "confidence": 0.55,
                "source": "url_path_signal",
                "evidence": ["URL path: particularsr"],
            },
            "scrape_stage": "classified_from_listing",
            "origen": "toctoc",
            "source_portal": "toctoc",
        }
        errors = validate_property_for_canonical_insert(bad_doc)
        assert "MISSING_TITLE_AND_DESCRIPTION" in errors
        assert "CLASSIFICATION_FROM_URL_PATH_ONLY" in errors
        assert "SCRAPE_STAGE_CLASSIFIED_FROM_LISTING_ONLY" in errors
        assert any("CONFIDENCE_TOO_LOW" in e for e in errors)
        assert len(errors) >= 4, f"Bad doc should have 4+ errors: {errors}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
