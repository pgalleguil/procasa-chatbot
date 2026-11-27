from pathlib import Path

import psutil

from scripts.benchmark_local_property_search import IndexProgress, load_progress


class FakeEmbedder:
    def __init__(self):
        self.encoded_texts = 0


def test_partial_index_progress_resumes_only_matching_snapshot(tmp_path: Path):
    state_path = tmp_path / "model.progress.json"
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
