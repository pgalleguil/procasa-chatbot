"""Tests precisos de umbrales: insercion y asignacion deben coincidir."""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pytest
from captacion_assignment_eligibility import assignment_eligibility

# Inline copy from mongo_store.py
def validate_classification_probability_consistency(state, confidence):
    errors = []
    if not state: return errors
    try: conf = float(confidence)
    except: return ["INVALID_CONFIDENCE"]
    if state == "INCIERTO":
        if conf < 0.50: errors.append(f"INCIERTO_CONFIDENCE_TOO_LOW({conf})")
        if conf >= 0.70: errors.append(f"INCIERTO_CONFIDENCE_TOO_HIGH({conf})")
    elif state == "DUEÑO_PROBABLE":
        if conf < 0.70: errors.append(f"DUEÑO_PROBABLE_CONFIDENCE_TOO_LOW({conf})")
        if conf >= 0.90: errors.append(f"DUEÑO_PROBABLE_CONFIDENCE_TOO_HIGH({conf})")
    elif state == "DUEÑO_SEGURO":
        if conf < 0.90: errors.append(f"DUEÑO_SEGURO_CONFIDENCE_TOO_LOW({conf})")
        if conf > 1.00: errors.append(f"DUEÑO_SEGURO_CONFIDENCE_TOO_HIGH({conf})")
    return errors

def vpci(state, confidence):
    return validate_classification_probability_consistency(state, confidence)

def make_doc(state, prob, **kw):
    base = {"listing_id":"1234567","url":"https://t.cl/t","comuna":"M","operacion":"v","tipo_propiedad":"c",
            "title":"T","description":"D","scrape_stage":"classification_done",
            "origen":"toctoc","comuna_slug":"m","source_portal":"toctoc",
            "classification":{"state":state,"confidence":prob,"source":"structural_rules",
            "assignment_ready":True,"owner_probability":prob,"evidence":["t"],"decision_source":"structural_rules"}}
    base.update(kw); return base

class TestProbabilityConsistency:
    # INCIERTO
    def test_incierto_050_valid(self): assert vpci("INCIERTO",0.50)==[]
    def test_incierto_069_valid(self): assert vpci("INCIERTO",0.69)==[]
    def test_incierto_070_rejected(self): assert any("HIGH" in e for e in vpci("INCIERTO",0.70))
    # DUEÑO_PROBABLE
    def test_dp_069_rejected(self): assert any("LOW" in e for e in vpci("DUEÑO_PROBABLE",0.69))
    def test_dp_070_valid(self): assert vpci("DUEÑO_PROBABLE",0.70)==[]
    def test_dp_089_valid(self): assert vpci("DUEÑO_PROBABLE",0.89)==[]
    def test_dp_090_rejected(self): assert any("HIGH" in e for e in vpci("DUEÑO_PROBABLE",0.90))
    # DUEÑO_SEGURO
    def test_ds_089_rejected(self): assert any("LOW" in e for e in vpci("DUEÑO_SEGURO",0.89))
    def test_ds_090_valid(self): assert vpci("DUEÑO_SEGURO",0.90)==[]
    def test_ds_100_valid(self): assert vpci("DUEÑO_SEGURO",1.00)==[]

class TestAssignmentConsistency:
    """Mismos umbrales en asignacion."""
    def test_incierto_050_assignable(self):
        ok,_=assignment_eligibility(make_doc("INCIERTO",0.50)); assert ok
    def test_incierto_069_assignable(self):
        ok,_=assignment_eligibility(make_doc("INCIERTO",0.69)); assert ok
    def test_incierto_070_rejected(self):
        ok,r=assignment_eligibility(make_doc("INCIERTO",0.70)); assert not ok
    def test_dp_069_rejected(self):
        ok,r=assignment_eligibility(make_doc("DUEÑO_PROBABLE",0.69)); assert not ok
    def test_dp_070_assignable(self):
        ok,_=assignment_eligibility(make_doc("DUEÑO_PROBABLE",0.70)); assert ok
    def test_dp_089_assignable(self):
        ok,_=assignment_eligibility(make_doc("DUEÑO_PROBABLE",0.89)); assert ok
    def test_dp_090_rejected(self):
        ok,r=assignment_eligibility(make_doc("DUEÑO_PROBABLE",0.90)); assert not ok
    def test_ds_089_rejected(self):
        ok,r=assignment_eligibility(make_doc("DUEÑO_SEGURO",0.89)); assert not ok
    def test_ds_090_assignable(self):
        ok,_=assignment_eligibility(make_doc("DUEÑO_SEGURO",0.90)); assert ok
    def test_ds_100_assignable(self):
        ok,_=assignment_eligibility(make_doc("DUEÑO_SEGURO",1.00)); assert ok
    def test_no_probability_rejected(self):
        doc=make_doc("DUEÑO_SEGURO",0.90)
        doc["classification"]["owner_probability"]=None
        ok,r=assignment_eligibility(doc); assert not ok
    def test_403_blocked_any_state(self):
        doc=make_doc("DUEÑO_SEGURO",0.95,scrape_stage="processing_blocked",block_reason="DETAIL_HTTP_403")
        ok,r=assignment_eligibility(doc); assert not ok
    def test_url_path_signal_rejected(self):
        doc=make_doc("DUEÑO_PROBABLE",0.75)
        doc["classification"]["source"]="url_path_signal"
        ok,r=assignment_eligibility(doc); assert not ok
