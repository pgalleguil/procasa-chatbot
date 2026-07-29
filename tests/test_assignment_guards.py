"""Tests adicionales: guard de asignacion, compatibilidad historica, rutas directas."""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from captacion_assignment_eligibility import assignment_eligibility

# Inline copy from mongo_store.py to avoid config import conflict
def validate_property_for_canonical_insert(doc):
    errors = []
    lid = doc.get("listing_id","")
    if not lid or not str(lid).isdigit(): errors.append("MISSING_LISTING_ID")
    if not doc.get("url"): errors.append("MISSING_URL")
    if not doc.get("comuna"): errors.append("MISSING_COMUNA")
    if not doc.get("operacion"): errors.append("MISSING_OPERACION")
    if not doc.get("tipo_propiedad"): errors.append("MISSING_TIPO_PROPIEDAD")
    t=doc.get("title",""); d=doc.get("description",doc.get("descripcion",""))
    if not t and not d: errors.append("MISSING_TITLE_AND_DESCRIPTION")
    c=doc.get("classification") or {}
    s=c.get("state",""); cf=c.get("confidence",0); sr=c.get("source","")
    if sr=="url_path_signal": errors.append("CLASSIFICATION_FROM_URL_PATH_ONLY")
    if not s: errors.append("MISSING_CLASSIFICATION_STATE")
    if not sr: errors.append("MISSING_CLASSIFICATION_SOURCE")
    if s=="DUEÑO_PROBABLE" and cf<0.70: errors.append(f"DUEÑO_PROBABLE_CONFIDENCE_TOO_LOW({cf})")
    if s=="DUEÑO_SEGURO" and cf<0.90: errors.append(f"DUEÑO_SEGURO_CONFIDENCE_TOO_LOW({cf})")
    ss=doc.get("scrape_stage","")
    if ss in ("PROCESSING_BLOCKED","needs_rescrape","ad_removed","incomplete"): errors.append(f"INVALID_SCRAPE_STAGE({ss})")
    if ss=="classified_from_listing": errors.append("SCRAPE_STAGE_CLASSIFIED_FROM_LISTING_ONLY")
    return errors

def make_doc(**overrides):
    base = {"listing_id":"1234567","url":"https://test.cl/1234567","comuna":"Maipu","comuna_slug":"maipu",
            "title":"Casa en venta","description":"Hermosa casa","operacion":"venta","tipo_propiedad":"casa",
            "scrape_stage":"classification_done","origen":"toctoc",
            "classification":{"state":"DUEÑO_SEGURO","confidence":0.95,"source":"structural_rules",
            "assignment_ready":True,"owner_probability":0.95,"evidence":["test"],"decision_source":"structural_rules"}}
    base.update(overrides); return base

class TestAssignmentGuard:
    def test_blocked_doc_not_assignable(self):
        doc=make_doc(scrape_stage="processing_blocked",block_reason="DETAIL_HTTP_403")
        ok,r=assignment_eligibility(doc); assert not ok; assert "removed_or_incomplete" in r
    def test_historical_incomplete_not_assignable(self):
        ok,r=assignment_eligibility(make_doc(title="",description="")); assert not ok
    def test_dueno_seguro_valid_assignable(self):
        ok,r=assignment_eligibility(make_doc()); assert ok
    def test_dueno_probable_valid_assignable(self):
        ok,r=assignment_eligibility(make_doc(classification={"state":"DUEÑO_PROBABLE","confidence":0.85,
            "source":"structural_rules","assignment_ready":True,"owner_probability":0.85,
            "evidence":["test"],"decision_source":"structural_rules"})); assert ok
    def test_corredor_not_assignable(self):
        ok,r=assignment_eligibility(make_doc(classification={"state":"CORREDOR_SEGURO","confidence":0.95,
            "source":"structural_rules","assignment_ready":False,"owner_probability":0.1}))
        assert not ok; assert "classification_not_assignable" in r
    def test_detail_403_not_assignable(self):
        ok,r=assignment_eligibility(make_doc(scrape_stage="processing_blocked",block_reason="DETAIL_HTTP_403",
            classification={"state":"INCIERTO","confidence":0.35,"source":"blocked","assignment_ready":False}))
        assert not ok
    def test_yapo_doc_not_affected(self):
        ok,r=assignment_eligibility(make_doc(origen="yapo")); assert ok
    def test_url_path_signal_not_assignable(self):
        ok,r=assignment_eligibility(make_doc(classification={"state":"DUEÑO_PROBABLE","confidence":0.55,
            "source":"url_path_signal","assignment_ready":True,"owner_probability":0.55}))
        assert "classification_from_url_path_only" in r

class TestInsertionGuardHistorical:
    def test_toctoc_valid(self):
        assert validate_property_for_canonical_insert(make_doc(scrape_stage="classification_done",
            classification={"state":"DUEÑO_SEGURO","confidence":0.95,"source":"structural_rules","evidence":["test"]}))==[]
    def test_yapo_valid(self):
        assert validate_property_for_canonical_insert(make_doc(origen="yapo",scrape_stage="processed",
            classification={"state":"DUEÑO_SEGURO","confidence":0.95,"source":"structural_rules","evidence":["test"]}))==[]
    def test_title_only_valid(self):
        assert validate_property_for_canonical_insert(make_doc(description="",
            classification={"state":"DUEÑO_SEGURO","confidence":0.95,"source":"structural_rules","evidence":["test"]}))==[]
    def test_desc_only_valid(self):
        assert validate_property_for_canonical_insert(make_doc(title="",
            classification={"state":"DUEÑO_SEGURO","confidence":0.95,"source":"structural_rules","evidence":["test"]}))==[]
    def test_removed_blocked(self):
        e=validate_property_for_canonical_insert(make_doc(scrape_stage="ad_removed",
            classification={"state":"AD_REMOVED","confidence":1.0,"source":"html_validation","evidence":["test"]}))
        assert any("INVALID_SCRAPE_STAGE" in x for x in e)
    def test_valid_passes(self):
        assert validate_property_for_canonical_insert(make_doc())==[]
