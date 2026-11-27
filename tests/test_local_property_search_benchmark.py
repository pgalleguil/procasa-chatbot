from pathlib import Path

import psutil

from scripts.benchmark_local_property_search import IndexProgress, load_progress
from scripts.aggregate_local_property_search_benchmark import ranking_agreement


class FakeEmbedder:
    def __init__(self):
        self.encoded_texts = 0


def test_partial_index_progress_resumes_only_matching_snapshot(tmp_path: Path):
    state_path = tmp_path / "model.sqlite"
    embedder = FakeEmbedder()
    first = IndexProgress(state_path, "model-revision", "snapshot-profiles-date",
                          None, embedder, "cpu", None, psutil, {}, 2.0)
    embedder.encoded_texts = 12
    first._save()
    first.stop.set()
    first.thread.join(timeout=2)

    saved = load_progress(state_path, "model-revision", "snapshot-profiles-date")
    assert saved["embedding_texts_encoded"] == 12
    assert saved["index_complete"] is False
    assert load_progress(state_path, "model-revision", "different-inputs") == {}

    resumed_embedder = FakeEmbedder()
    resumed_embedder.encoded_texts = 3
    resumed = IndexProgress(state_path, "model-revision", "snapshot-profiles-date",
                            None, resumed_embedder, "cpu", None, psutil, saved, 4.0)
    totals = resumed.finish()
    assert totals["resumed"] is True
    assert totals["embedding_texts_encoded"] == 15
    final = load_progress(state_path, "model-revision", "snapshot-profiles-date")
    assert final["index_complete"] is True
    assert final["model_load_seconds"] == 4.0


def test_ranking_agreement_is_top10_overlap_not_relevance():
    profiles = [{"profile_id": "p1"}, {"profile_id": "p2"}]
    baseline = {
        "p1": {"compatible": [], "review": [{"listing_key": "a"}, {"listing_key": "b"}]},
        "p2": {"compatible": [], "review": [{"listing_key": "c"}]},
    }
    candidate = {
        "p1": {"compatible": [], "review": [{"listing_key": "a"}, {"listing_key": "x"}]},
        "p2": {"compatible": [], "review": [{"listing_key": "c"}]},
    }
    result = ranking_agreement(baseline, candidate, profiles)
    assert result["profile_groups_compared"] == 2
    assert result["mean_top10_set_jaccard_vs_minilm"] == 0.6667
    assert result["top1_match_rate_vs_minilm"] == 1.0
    assert result["exact_top10_list_match_groups"] == 1
