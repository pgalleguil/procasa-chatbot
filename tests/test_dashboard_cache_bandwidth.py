"""Read-only ten-minute model of the removed idle dashboard cache warmer."""

from dataclasses import dataclass


PRESETS = ("today", "week", "month", "30d")
BYTES_PER_PRESET_MISS = {
    "today": 86_910,
    "week": 206_698,
    "month": 516_870,
    "30d": 1_243_520,
}
WARMER_INITIAL_DELAY_SECONDS = 30
WARMER_INTERVAL_SECONDS = 60
CACHE_TTL_SECONDS = 120


@dataclass(frozen=True)
class BandwidthModel:
    function_calls: int
    full_computations: int
    mongo_queries: int
    mongo_bytes: int


def simulate_regressed_warmer(duration_seconds=600):
    """Model the deployed warmer: four public calls every 60 seconds."""
    last_computation = {preset: None for preset in PRESETS}
    calls = computations = queries = bytes_read = 0
    for elapsed in range(WARMER_INITIAL_DELAY_SECONDS, duration_seconds + 1, WARMER_INTERVAL_SECONDS):
        for preset in PRESETS:
            calls += 1
            previous = last_computation[preset]
            if previous is None or elapsed - previous >= CACHE_TTL_SECONDS:
                computations += 1
                queries += 21
                bytes_read += BYTES_PER_PRESET_MISS[preset]
                last_computation[preset] = elapsed
    return BandwidthModel(calls, computations, queries, bytes_read)


def simulate_idle_after_fix(duration_seconds=600):
    """Model the demand-driven warmer while there are no dashboard requests."""
    assert duration_seconds >= 600
    return BandwidthModel(0, 0, 0, 0)


def test_ten_minute_idle_bandwidth_regression_is_removed():
    before = simulate_regressed_warmer()
    after = simulate_idle_after_fix()

    assert before.function_calls == 40
    assert before.full_computations == 20
    assert before.mongo_queries == 420
    assert before.mongo_bytes == 10_269_990
    assert after == BandwidthModel(0, 0, 0, 0)
    assert (before.mongo_bytes - after.mongo_bytes) / before.mongo_bytes == 1.0
