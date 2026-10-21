"""Regression tests for the indexable CRM assignment-cycle lookup.

The join must accept both ObjectId and legacy string lead_id values without
converting the foreign indexed field for each lead.
"""
import inspect

import api_crm


def test_mixed_id_keys_preserve_supported_types():
    stage = api_crm._crm_indexed_lead_join_keys_stage()
    expression = stage["$set"]["_crm_lead_join_ids"]
    assert expression["$cond"][0] == {
        "$in": [{"$type": "$_id"}, ["objectId", "string"]],
    }
    assert expression["$cond"][1] == ["$_id", {"$toString": "$_id"}]
    assert expression["$cond"][2] == []


def test_cycle_lookup_uses_foreign_lead_id_index():
    lookup = api_crm._canonical_active_cycle_lookup_stage()["$lookup"]
    assert lookup["from"] == "crm_assignment_cycles"
    assert lookup["localField"] == "_crm_lead_join_ids"
    assert lookup["foreignField"] == "lead_id"
    assert lookup["as"] == "_active_cycle"
    match_conditions = lookup["pipeline"][0]["$match"]["$and"]
    assert match_conditions[0]["$expr"]["$in"] == [
        {"$type": "$lead_id"}, ["objectId", "string"],
    ]
    assert match_conditions[1]["cycle_status"] == "active"
    assert match_conditions[1]["schema_version"] == "crm_assignment_cycle_v1"
    assert match_conditions[1]["unassigned_at"] is None
    assert match_conditions[2]["$or"][0] == {"notification_eligible": True}
    assert match_conditions[2]["$or"][1] == api_crm._administrative_cycle_lookup_predicate()
    assert lookup["pipeline"][-1] == {"$limit": 1}


def test_list_builds_index_join_keys_before_lookup():
    source = inspect.getsource(api_crm.get_crm_leads_list)
    assert source.count("_crm_indexed_lead_join_keys_stage(),") == 1
    assert source.count("_canonical_active_cycle_lookup_stage(),") == 1
    assert source.index("_crm_indexed_lead_join_keys_stage(),") < source.index(
        "_canonical_active_cycle_lookup_stage(),"
    )
