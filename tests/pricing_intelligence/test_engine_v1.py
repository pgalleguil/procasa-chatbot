from datetime import datetime, timezone

import pytest

from analytics.pricing_intelligence.engine_v1 import evaluate_engine_v1


AS_OF = "2026-09-20T23:00:00+00:00"


def make_input(
    *,
    operation="venta",
    current=7000,
    n=39,
    p60=5600,
    p75=6050,
    p90=9793.82,
    signal_30d="ZERO_UNCERTAIN",
    signal_90d="OBSERVED_POSITIVE",
    confidence_30d="unknown",
    confidence_90d="medium",
    exposure_state="ADEQUATE",
    deterministic=True,
    fingerprint="fp",
    robustness_warning=False,
    last_price_change_at=None,
    operation_ambiguity=False,
    geography_valid=True,
    critical_conflict=False,
):
    return {
        "operation": operation,
        "current_price_uf": current,
        "cohort": {
            "n": n,
            "p60": p60,
            "p75": p75,
            "p90": p90,
            "ecdf": 84.62,
            "deterministic": deterministic,
            "fingerprint": fingerprint,
            "robustness_warning": robustness_warning,
        },
        "commercial_response": {
            "inquiries_30d": 0,
            "inquiries_90d": 10,
            "signal_30d": signal_30d,
            "signal_90d": signal_90d,
            "confidence_30d": confidence_30d,
            "confidence_90d": confidence_90d,
        },
        "exposure": {"state": exposure_state, "active_portals": ("portal_inmobiliario", "toctoc")},
        "observation": {
            "window_quality_30d": "UNKNOWN",
            "window_quality_90d": "UNKNOWN",
            "source_coverage_30d": "UNKNOWN",
            "source_coverage_90d": "PARTIAL",
        },
        "context": {
            "page_as_of": AS_OF,
            "last_price_change_at": last_price_change_at,
            "operation_ambiguity": operation_ambiguity,
            "geography_valid": geography_valid,
            "critical_conflict": critical_conflict,
        },
    }


def evaluate(**overrides):
    values = make_input(**overrides)
    return evaluate_engine_v1(**values)


def test_6464_control_case():
    decision = evaluate()
    assert decision.status == "READY"
    assert decision.market_position == "HIGH"
    assert decision.gap_to_p75_pct == 13.57
    assert decision.recommendation == "MARKET_REPOSITIONING"
    assert decision.eligibility == "AMBER"
    assert decision.confidence == "MEDIUM"
    assert decision.gradual_price_uf == 6500
    assert decision.competitive_reference_uf == 6050
    assert decision.owner_action == "DISCUSS_WITH_EXECUTIVE"


def test_6786_trivial_gap_maintains_without_target():
    decision = evaluate(current=3720, n=20, p60=3600, p75=3695, p90=4200)
    assert decision.market_position == "HIGH"
    assert decision.gap_to_p75_pct == 0.67
    assert decision.recommendation == "MAINTAIN"
    assert decision.gradual_price_uf is None
    assert decision.competitive_reference_uf is None
    assert decision.owner_action == "NO_ACTION"


def test_6414_insufficient_cohort_is_red_and_has_required_reason():
    decision = evaluate(current=5000, n=7, p60=4000, p75=4500, p90=6000)
    assert decision.recommendation == "EXECUTIVE_REVIEW"
    assert decision.eligibility == "RED"
    assert decision.confidence == "LOW"
    assert decision.market_position == "UNAVAILABLE"
    assert decision.owner_action == "NO_ACTION"
    assert "INSUFFICIENT_MARKET_COMPARABLES" in decision.reasons
    assert decision.gradual_price_uf is None


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (5600, "ALIGNED"),  # P60 exact
        (6050, "SLIGHTLY_HIGH"),  # P75 exact
        (9793.82, "HIGH"),  # P90 exact
        (9793.83, "VERY_HIGH"),
    ],
)
def test_market_position_boundaries_are_inclusive(current, expected):
    assert evaluate(current=current).market_position == expected


@pytest.mark.parametrize(
    ("gap", "recommendation", "action"),
    [
        (1.99, "MAINTAIN", "NO_ACTION"),
        (2.00, "MARKET_REPOSITIONING", "CAN_REQUEST_PRICE_CHANGE"),
        (8.00, "MARKET_REPOSITIONING", "CAN_REQUEST_PRICE_CHANGE"),
        (8.01, "MARKET_REPOSITIONING", "DISCUSS_WITH_EXECUTIVE"),
        (12.00, "MARKET_REPOSITIONING", "DISCUSS_WITH_EXECUTIVE"),
        (12.01, "MARKET_REPOSITIONING", "DISCUSS_WITH_EXECUTIVE"),
        (15.00, "MARKET_REPOSITIONING", "DISCUSS_WITH_EXECUTIVE"),
    ],
)
def test_gap_bands_control_recommendation_and_action(gap, recommendation, action):
    current = 5000.0
    p75 = current * (1 - gap / 100)
    decision = evaluate(
        current=current,
        p60=p75 * 0.9,
        p75=p75,
        p90=current * 1.4,
        confidence_30d="high",
        confidence_90d="high",
    )
    assert decision.recommendation == recommendation
    assert decision.owner_action == action
    assert decision.gradual_price_uf is None if recommendation == "MAINTAIN" else decision.gradual_price_uf <= current


@pytest.mark.parametrize("n", [8, 19])
def test_moderate_cohort_sizes_do_not_generate_numeric_targets(n):
    decision = evaluate(current=5000, n=n, p60=4000, p75=4500, p90=7000, confidence_30d="high", confidence_90d="high")
    assert decision.recommendation == "EXECUTIVE_REVIEW"
    assert decision.eligibility == "AMBER"
    assert decision.confidence == "LOW"
    assert decision.gradual_price_uf is None
    assert decision.competitive_reference_uf is None
    assert decision.owner_action == "DISCUSS_WITH_EXECUTIVE"


def test_n20_high_quality_cohort_can_generate_numeric_target():
    decision = evaluate(
        current=5000,
        n=20,
        p60=4000,
        p75=4500,
        p90=7000,
        signal_30d="OBSERVED_POSITIVE",
        signal_90d="OBSERVED_POSITIVE",
        confidence_30d="medium",
        confidence_90d="medium",
    )
    assert decision.recommendation == "MARKET_REPOSITIONING"
    assert decision.eligibility == "GREEN"
    assert decision.confidence == "HIGH"
    assert decision.gradual_price_uf is not None


def test_n20_green_case_can_request_but_does_not_mutate_price():
    decision = evaluate(current=5000, n=20, p60=4000, p75=4750, p90=7000, confidence_30d="high", confidence_90d="high")
    assert decision.owner_action == "CAN_REQUEST_PRICE_CHANGE"
    assert decision.gradual_price_uf == 4850
    assert decision.gradual_price_uf <= 4875


def test_rounding_never_exceeds_mathematical_target():
    decision = evaluate()
    mathematical = (7000 + 6050) / 2
    assert decision.gradual_price_uf == 6500
    assert decision.gradual_price_uf <= mathematical
    assert decision.gradual_price_uf <= 7000


def test_robustness_warning_lowers_eligibility_and_requestability():
    decision = evaluate(
        current=5000,
        n=20,
        p60=4000,
        p75=4500,
        p90=7000,
        robustness_warning=True,
        confidence_30d="high",
        confidence_90d="high",
    )
    assert decision.recommendation == "EXECUTIVE_REVIEW"
    assert decision.eligibility == "AMBER"
    assert decision.gradual_price_uf is None
    assert decision.competitive_reference_uf is None
    assert decision.owner_action == "DISCUSS_WITH_EXECUTIVE"
    assert decision.confidence == "LOW"


def test_high_confidence_is_reachable_with_observed_reliable_evidence():
    decision = evaluate(
        signal_30d="OBSERVED_POSITIVE",
        signal_90d="OBSERVED_ZERO_COMPLETE",
        confidence_30d="medium",
        confidence_90d="high",
    )
    assert decision.confidence == "HIGH"


def test_unknown_demand_can_remain_medium_on_high_quality_cohort():
    decision = evaluate(
        signal_30d="ZERO_UNCERTAIN",
        signal_90d="ZERO_UNCERTAIN",
        confidence_30d="unknown",
        confidence_90d="unknown",
    )
    assert decision.confidence == "MEDIUM"


def test_amber_never_becomes_requestable():
    decision = evaluate(
        current=7000,
        n=20,
        p60=5600,
        p75=6050,
        p90=9793.82,
        signal_30d="OBSERVED_POSITIVE",
        signal_90d="OBSERVED_POSITIVE",
        confidence_30d="medium",
        confidence_90d="medium",
    )
    assert decision.eligibility == "AMBER"
    assert decision.owner_action == "DISCUSS_WITH_EXECUTIVE"
    assert decision.owner_action != "CAN_REQUEST_PRICE_CHANGE"


def test_partial_and_low_exposure_are_conservative():
    partial = evaluate(current=5000, p60=4000, p75=4500, p90=7000, exposure_state="PARTIAL")
    low = evaluate(current=5000, p60=4000, p75=4500, p90=7000, exposure_state="LOW")
    assert partial.recommendation == "MARKET_REPOSITIONING"
    assert partial.owner_action == "DISCUSS_WITH_EXECUTIVE"
    assert low.recommendation == "IMPROVE_EXPOSURE"
    assert low.gradual_price_uf is None


def test_cooldown_blocks_new_reduction():
    decision = evaluate(last_price_change_at="2026-09-01T12:00:00+00:00")
    assert decision.recommendation == "OBSERVE"
    assert decision.gradual_price_uf is None
    assert decision.owner_action == "NO_ACTION"


def test_strong_recent_demand_does_not_trigger_reduction():
    decision = evaluate(signal_30d="STRONG_RECENT", confidence_30d="high")
    assert decision.recommendation == "OBSERVE"
    assert decision.gradual_price_uf is None
    assert decision.owner_action == "NO_ACTION"


def test_zero_uncertain_never_causes_reduction_by_itself():
    decision = evaluate(signal_30d="ZERO_UNCERTAIN")
    assert decision.recommendation == "MARKET_REPOSITIONING"
    assert decision.gradual_price_uf == 6500
    assert "ZERO_UNCERTAIN_NOT_PRICING_EVIDENCE" in decision.warnings


def test_rent_never_generates_pricing_target():
    decision = evaluate(operation="arriendo", current=5000, p60=4000, p75=4500, p90=7000)
    assert decision.recommendation == "EXECUTIVE_REVIEW"
    assert decision.eligibility == "RED"
    assert decision.gradual_price_uf is None
    assert decision.competitive_reference_uf is None
    assert decision.owner_action == "NO_ACTION"


def test_ambiguous_dual_operation_is_red():
    decision = evaluate(operation="venta", operation_ambiguity=True)
    assert decision.recommendation == "EXECUTIVE_REVIEW"
    assert decision.eligibility == "RED"
    assert decision.owner_action == "NO_ACTION"


def test_same_input_is_deterministic():
    first = evaluate().to_dict()
    second = evaluate().to_dict()
    assert first == second


def test_invalid_geography_is_red_without_target():
    decision = evaluate(geography_valid=False)
    assert decision.eligibility == "RED"
    assert decision.recommendation == "EXECUTIVE_REVIEW"
    assert decision.gradual_price_uf is None
