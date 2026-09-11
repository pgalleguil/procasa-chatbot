from scripts.prepare_crm_sla_shadow_infrastructure import (
    REQUIRED_INDEXES,
    _classify_index,
)


def _spec(object_name="TEST", keys=(("a", 1), ("b", 1))):
    return {"object": object_name, "keys": keys}


def _meta(indexes):
    return {"exists": True, "indexes": indexes}


def _validation(*fields):
    return {"all_found": True, "found": {field: True for field in fields}, "errors": {}}


def test_required_indexes_are_additive_and_non_unique():
    assert len(REQUIRED_INDEXES) == 4
    assert all(spec["required_phase"] == "REQUIRED_BEFORE_SHADOW" for spec in REQUIRED_INDEXES)


def test_exact_existing_is_usable():
    spec = _spec(keys=(("a", 1), ("b", 1)))
    meta = _meta([{"name": "idx_existing", "key": {"a": 1, "b": 1}}])
    assert _classify_index(spec, meta, _validation("a", "b")) == "EXACT_EXISTING"


def test_longer_existing_index_is_prefix_usable():
    spec = _spec(keys=(("a", 1), ("b", 1)))
    meta = _meta([{"name": "idx_existing", "key": {"a": 1, "b": 1, "c": 1}}])
    assert _classify_index(spec, meta, _validation("a", "b")) == "PREFIX_USABLE"


def test_same_fields_with_different_direction_is_conflicting():
    spec = _spec(keys=(("a", 1), ("b", 1)))
    meta = _meta([{"name": "idx_existing", "key": {"a": -1, "b": 1}}])
    assert _classify_index(spec, meta, _validation("a", "b")) == "CONFLICTING_INDEX"


def test_missing_equivalent_index_requires_new_index():
    spec = _spec(keys=(("a", 1), ("b", 1)))
    assert _classify_index(spec, _meta([]), _validation("a", "b")) == "NEEDS_NEW_INDEX"


def test_missing_field_blocks_creation():
    spec = _spec(keys=(("a", 1), ("b", 1)))
    validation = {"all_found": False, "found": {"a": True, "b": False}, "errors": {}}
    assert _classify_index(spec, _meta([]), validation) == "FIELD_NOT_VALIDATED"


def test_partial_equivalent_is_not_accepted_as_usable():
    spec = _spec(keys=(("a", 1), ("b", 1)))
    meta = _meta([{"name": "idx_partial", "key": {"a": 1, "b": 1}, "partialFilterExpression": {"a": {"$exists": True}}}])
    assert _classify_index(spec, meta, _validation("a", "b")) == "CONFLICTING_INDEX"
