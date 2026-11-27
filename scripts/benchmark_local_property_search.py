"""Compare the four local property-search variants on one fixed snapshot."""
from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MINILM = ("sentence-transformers/all-MiniLM-L6-v2", "1110a243fdf4706b3f48f1d95db1a4f5529b4d41")
BGE_M3 = ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181")
BGE_RERANKER = ("BAAI/bge-reranker-v2-m3", "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e")


class MemorySampler:
    def __init__(self, device, torch, psutil):
        self.device = device
        self.torch = torch
        self.process = psutil.Process()
        self.samples = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        while not self.stop.is_set():
            self._sample_once()
            self.stop.wait(0.1)

    def _sample_once(self):
        try:
            rss = self.process.memory_info().rss
            allocated = reserved = 0
            if self.device == "cuda" and self.torch.cuda.is_available():
                allocated = self.torch.cuda.memory_allocated()
                reserved = self.torch.cuda.memory_reserved()
            self.samples.append((rss, allocated, reserved))
        except Exception:
            pass

    def __enter__(self):
        self._sample_once()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=2)
        self._sample_once()

    def summary(self):
        if not self.samples:
            return {"peak_rss_mb": None, "peak_gpu_allocated_mb": None, "peak_gpu_reserved_mb": None}
        return {
            "peak_rss_mb": round(max(s[0] for s in self.samples) / 1024**2, 1),
            "peak_gpu_allocated_mb": round(max(s[1] for s in self.samples) / 1024**2, 1),
            "peak_gpu_reserved_mb": round(max(s[2] for s in self.samples) / 1024**2, 1),
        }


def load_progress(path, namespace, run_identity):
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(path, timeout=10) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmark_progress'"
            ).fetchone()
            if not table:
                return {}
            row = db.execute(
                "SELECT state_json FROM benchmark_progress WHERE namespace=? AND run_identity=?",
                (namespace, run_identity),
            ).fetchone()
        if not row:
            return {}
        state = json.loads(row[0])
    except (OSError, sqlite3.Error, json.JSONDecodeError):
        return {}
    return state if (state.get("namespace") == namespace and state.get("run_identity") == run_identity
                     and state.get("schema") == 1) else {}


class IndexProgress:
    """Persist cumulative index work so a terminated local run can resume."""
    def __init__(self, path, namespace, run_identity, cache_path, embedder, device, torch, psutil,
                 prior, model_load_seconds):
        self.path = path
        self.namespace = namespace
        self.run_identity = run_identity
        self.cache_path = cache_path
        self.embedder = embedder
        self.device = device
        self.torch = torch
        self.process = psutil.Process()
        self.prior = prior
        self.prior_index_seconds = float(prior.get("index_elapsed_seconds", 0.0))
        self.prior_encoded = int(prior.get("embedding_texts_encoded", 0))
        self.prior_memory = prior.get("memory_peaks") or {}
        self.model_load_seconds = model_load_seconds
        self.started = time.perf_counter()
        self.stop = threading.Event()
        self.thread = None
        self.index_complete = bool(prior.get("index_complete"))
        if not self.index_complete:
            self.thread = threading.Thread(target=self._watch, daemon=True)
            self._save()
            self.thread.start()

    def _cache_count(self):
        if not self.cache_path or not self.cache_path.exists():
            return 0
        try:
            with sqlite3.connect(self.cache_path, timeout=1) as db:
                return int(db.execute(
                    "SELECT COUNT(*) FROM vectors WHERE namespace=?", (self.namespace,)
                ).fetchone()[0])
        except (sqlite3.Error, OSError):
            return None

    def _memory(self):
        result = {
            "peak_rss_mb": round(self.process.memory_info().rss / 1024**2, 1),
            "peak_gpu_allocated_mb": 0.0,
            "peak_gpu_reserved_mb": 0.0,
        }
        if self.device == "cuda" and self.torch is not None and self.torch.cuda.is_available():
            result["peak_gpu_allocated_mb"] = round(self.torch.cuda.memory_allocated() / 1024**2, 1)
            result["peak_gpu_reserved_mb"] = round(self.torch.cuda.memory_reserved() / 1024**2, 1)
        for key, value in self.prior_memory.items():
            if value is not None:
                result[key] = max(result.get(key) or 0, value)
        return result

    def _save(self, *, complete=None):
        elapsed = self.prior_index_seconds
        if not self.index_complete:
            elapsed += time.perf_counter() - self.started
        state = {
            "schema": 1, "namespace": self.namespace, "run_identity": self.run_identity,
            "elapsed_reconstruction_estimated": bool(
                self.prior.get("elapsed_reconstruction_estimated")
            ),
            "index_elapsed_seconds": round(elapsed, 3),
            "model_load_seconds": round(self.model_load_seconds, 3),
            "embedding_texts_encoded": self.prior_encoded + self.embedder.encoded_texts,
            "cache_vector_count": self._cache_count(),
            "memory_peaks": self._memory(),
            "index_complete": self.index_complete if complete is None else complete,
            "updated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        serialized = json.dumps(state, separators=(",", ":"))
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS benchmark_progress "
                "(namespace TEXT, run_identity TEXT, state_json TEXT, "
                "PRIMARY KEY(namespace,run_identity))"
            )
            db.execute(
                "INSERT OR REPLACE INTO benchmark_progress VALUES (?,?,?)",
                (self.namespace, self.run_identity, serialized),
            )

    def _watch(self):
        while not self.stop.wait(2):
            try:
                self._save()
            except Exception:
                pass

    def finish(self):
        if not self.index_complete:
            self.stop.set()
            if self.thread:
                self.thread.join(timeout=2)
            self._save(complete=True)
            self.index_complete = True
        return {
            "index_elapsed_seconds": self.prior_index_seconds +
                (0 if self.prior.get("index_complete") else time.perf_counter() - self.started),
            "embedding_texts_encoded": self.prior_encoded + self.embedder.encoded_texts,
            "cache_vector_count": self._cache_count(),
            "memory_peaks": self._memory(),
            "resumed": bool(self.prior) and not bool(self.prior.get("index_complete")),
            "elapsed_estimated": bool(self.prior.get("elapsed_reconstruction_estimated")),
        }

    def suspend(self):
        if not self.index_complete:
            self.stop.set()
            if self.thread:
                self.thread.join(timeout=2)
            self._save()


class Embedder:
    def __init__(self, model_id, revision, device, batch_size, torch, sentence_transformers):
        self.device = device
        self.batch_size = batch_size
        self.torch = torch
        self.encoded_texts = 0
        model_kwargs = {"torch_dtype": torch.float16} if device == "cuda" else None
        self.model = sentence_transformers.SentenceTransformer(
            model_id, revision=revision, device=device, local_files_only=False,
            trust_remote_code=False, model_kwargs=model_kwargs)

    def encode(self, texts):
        self.encoded_texts += len(texts)
        values = self.model.encode(texts, batch_size=self.batch_size,
                                   normalize_embeddings=True, show_progress_bar=False,
                                   convert_to_numpy=True)
        if self.device == "cuda":
            self.torch.cuda.synchronize()
        return values


class Reranker:
    def __init__(self, model_id, revision, device, batch_size, torch, sentence_transformers):
        self.device = device
        self.batch_size = batch_size
        self.torch = torch
        model_kwargs = {"torch_dtype": torch.float16} if device == "cuda" else None
        self.model = sentence_transformers.CrossEncoder(
            model_id, revision=revision, device=device, local_files_only=False,
            trust_remote_code=False, model_kwargs=model_kwargs)

    def score(self, query, texts):
        values = self.model.predict([(query, text) for text in texts],
                                    batch_size=self.batch_size, show_progress_bar=False)
        if self.device == "cuda":
            self.torch.cuda.synchronize()
        return values


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def synchronize(device, torch):
    if device == "cuda":
        torch.cuda.synchronize()


def run_variant(spec, rows, profiles, as_of, output_dir, requested_device,
                rerank_pool, resume_partial, run_identity,
                torch, np, psutil, sentence_transformers):
    from chatbot.local_embedding_cache import EmbeddingCache, model_namespace
    from chatbot.local_property_search import LocalPropertyIndex, filter_property

    progress_trackers = []

    def attempt(device):
        embedding_batch_size = spec.get("embedding_batch_size")
        reranker_batch_size = spec.get("reranker_batch_size")
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
        process = psutil.Process()
        rss_before = process.memory_info().rss
        sampler = MemorySampler(device, torch, psutil)
        results = {}
        per_query_ms = []
        excluded = {}
        eligible_counts = {"compatible": 0, "review": 0}
        compatible_rows = 0
        hard_violations = 0
        external_compatible = 0
        model_load_seconds = 0.0
        cache_path = None
        progress = None
        progress_metrics = None
        namespace = None
        cold_encoded = hot_encoded = update_encoded = 0
        update_seconds = None
        cache_bytes = 0
        index = None

        with sampler:
            start_load = time.perf_counter()
            embedder = None
            reranker = None
            if spec.get("embedding"):
                model_id, revision = spec["embedding"]
                namespace = model_namespace(model_id, revision,
                                            version=sentence_transformers.__version__)
                cache_path = output_dir / "cache" / f"{spec['name']}-{device}.sqlite"
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                prior_progress = load_progress(cache_path, namespace, run_identity) if resume_partial else {}
                if cache_path.exists() and not resume_partial:
                    raise FileExistsError(f"Refusing to reuse an existing benchmark cache: {cache_path}")
                if cache_path.exists() and resume_partial and not prior_progress:
                    raise ValueError(f"Cannot safely resume cache without a matching progress file: {cache_path}")
                embedder = Embedder(model_id, revision, device, embedding_batch_size,
                                    torch, sentence_transformers)
                # Keep cache commits bounded but avoid hundreds of tiny model.encode calls.
                cache = EmbeddingCache(cache_path, namespace, batch_size=512)
            else:
                cache = None
                prior_progress = {}
            if spec.get("reranker"):
                model_id, revision = spec["reranker"]
                reranker = Reranker(model_id, revision, device, reranker_batch_size,
                                    torch, sentence_transformers)
            model_load_seconds = float(prior_progress.get("model_load_seconds", 0.0)) + time.perf_counter() - start_load

            if device == "cuda":
                torch.cuda.reset_peak_memory_stats()
            if embedder:
                progress = IndexProgress(
                    cache_path, namespace, run_identity, cache_path, embedder, device, torch,
                    psutil, prior_progress, model_load_seconds,
                )
                progress_trackers.append(progress)
            cold_start = time.perf_counter()
            index = LocalPropertyIndex(rows, embedder, cache=cache,
                                       reranker=reranker, rerank_pool=rerank_pool)
            synchronize(device, torch)
            if progress:
                progress_metrics = progress.finish()
                cold_index_seconds = progress_metrics["index_elapsed_seconds"]
                cold_encoded = progress_metrics["cache_vector_count"] or progress_metrics["embedding_texts_encoded"]
            else:
                cold_index_seconds = time.perf_counter() - cold_start
                cold_encoded = 0
            rss_after_index = process.memory_info().rss

            hot_encoded_start = embedder.encoded_texts if embedder else 0
            hot_start = time.perf_counter()
            hot_index = LocalPropertyIndex(rows, embedder, cache=cache,
                                           reranker=reranker, rerank_pool=rerank_pool)
            synchronize(device, torch)
            hot_index_seconds = time.perf_counter() - hot_start
            hot_encoded = (embedder.encoded_texts - hot_encoded_start) if embedder else 0

            if embedder:
                changed = [dict(row) for row in rows]
                changed[0]["search_text"] = (changed[0].get("search_text") or "") + " cache update probe"
                update_encoded_start = embedder.encoded_texts
                update_start = time.perf_counter()
                updated_index = LocalPropertyIndex(changed, embedder, cache=cache,
                                                   reranker=reranker, rerank_pool=rerank_pool)
                synchronize(device, torch)
                update_seconds = time.perf_counter() - update_start
                update_encoded = embedder.encoded_texts - update_encoded_start
                del updated_index, changed
            else:
                changed = [dict(row) for row in rows]
                changed[0]["search_text"] = (changed[0].get("search_text") or "") + " cache update probe"
                update_start = time.perf_counter()
                updated_index = LocalPropertyIndex(changed, reranker=reranker,
                                                   rerank_pool=rerank_pool)
                update_seconds = time.perf_counter() - update_start
                del updated_index, changed

            row_by_key = {row["listing_key"]: row for row in rows}
            for profile in profiles:
                synchronize(device, torch)
                query_start = time.perf_counter()
                result = hot_index.search(profile, limit=10, now=as_of)
                synchronize(device, torch)
                per_query_ms.append((time.perf_counter() - query_start) * 1000)
                results[profile["profile_id"]] = result
                for name, count in result["excluded"].items():
                    excluded[name] = excluded.get(name, 0) + count
                for group in ("compatible", "review"):
                    eligible_counts[group] += result[group + "_count"]
                    for item in result[group]:
                        if group != "compatible":
                            continue
                        compatible_rows += 1
                        row = row_by_key.get(item["listing_key"], {})
                        state, _ = filter_property(profile, row, as_of)
                        if state != "compatible":
                            hard_violations += 1
                        if row.get("source_type") != "PROCASA":
                            external_compatible += 1

            repeat = hot_index.search(profiles[0], limit=10, now=as_of)
            stable_order = all(
                [x["listing_key"] for x in results[profiles[0]["profile_id"]][g]]
                == [x["listing_key"] for x in repeat[g]]
                for g in ("compatible", "review")
            )
            if cache_path and cache_path.exists():
                cache_bytes = cache_path.stat().st_size

            embedding_spec = spec.get("embedding") or (None, None)
            reranker_spec = spec.get("reranker") or (None, None)
            model_id, embedding_revision = embedding_spec
            reranker_id, reranker_revision = reranker_spec
            query_total_seconds = sum(per_query_ms) / 1000
            metrics = {
                "variant": spec["name"], "embedding_model": model_id,
                "embedding_revision": embedding_revision, "reranker_model": reranker_id,
                "reranker_revision": reranker_revision, "device": device,
                "embedding_batch_size": embedding_batch_size,
                "reranker_batch_size": reranker_batch_size if reranker else None,
                "precision": "float16" if device == "cuda" else "float32",
                "profiles": len(profiles), "catalog_rows": len(rows),
                "model_load_seconds": round(model_load_seconds, 3),
                "index_cold_seconds": round(cold_index_seconds, 3),
                "cold_index_resumed_from_checkpoint": progress_metrics["resumed"] if progress_metrics else False,
                "cold_index_elapsed_estimated": progress_metrics["elapsed_estimated"] if progress_metrics else False,
                "index_warm_seconds": round(hot_index_seconds, 3),
                "single_listing_update_seconds": round(update_seconds, 3) if update_seconds is not None else None,
                "embedding_texts_encoded_cold": cold_encoded,
                "embedding_texts_encoded_warm": hot_encoded,
                "embedding_texts_encoded_single_listing_update": update_encoded,
                "embedding_cache_bytes": cache_bytes,
                "embedding_cache_commit_batch_size": 512 if embedder else None,
                "query_latency_mean_ms": round(statistics.mean(per_query_ms), 2),
                "query_latency_p50_ms": round(percentile(per_query_ms, 0.50), 2),
                "query_latency_p95_ms": round(percentile(per_query_ms, 0.95), 2),
                "query_throughput_per_second": round(len(profiles) / query_total_seconds, 3) if query_total_seconds else None,
                "compatible_candidates_across_queries": eligible_counts["compatible"],
                "review_candidates_across_queries": eligible_counts["review"],
                "returned_compatible_rows_top10": compatible_rows,
                "hard_requirement_violations_in_compatible": hard_violations,
                "external_rows_in_compatible": external_compatible,
                "excluded_counts_across_queries": excluded,
                "stable_order_for_repeat_query": stable_order,
                "rss_before_mb": round(rss_before / 1024**2, 1),
                "rss_after_index_mb": round(rss_after_index / 1024**2, 1),
                "memory_peaks": {
                    key: max(sampler.summary().get(key) or 0,
                             (progress_metrics["memory_peaks"].get(key) or 0) if progress_metrics else 0)
                    for key in ("peak_rss_mb", "peak_gpu_allocated_mb", "peak_gpu_reserved_mb")
                },
            }
        del index
        if "hot_index" in locals():
            del hot_index
        if "embedder" in locals():
            del embedder
        if "reranker" in locals():
            del reranker
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return metrics, results

    initial_device = requested_device if spec.get("embedding") or spec.get("reranker") else "cpu"
    try:
        return attempt(initial_device)
    except RuntimeError as exc:
        message = str(exc).casefold()
        if initial_device == "cuda" and ("out of memory" in message or "cuda error" in message):
            for tracker in progress_trackers:
                tracker.suspend()
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            metrics, results = attempt("cpu")
            metrics["cuda_fallback_reason"] = str(exc)[:240]
            return metrics, results
        raise


def write_blind_review(profiles, all_results, output_dir):
    candidates = {}
    keys = []
    for variant, profile_results in all_results.items():
        for profile in profiles:
            result = profile_results[profile["profile_id"]]
            for group in ("compatible", "review"):
                for rank, item in enumerate(result[group][:10], 1):
                    key = (profile["profile_id"], item["listing_key"])
                    if key not in candidates:
                        candidates[key] = {
                            "profile_id": profile["profile_id"],
                            "operation": profile["operation"],
                            "communes": " | ".join(profile["comunas"]),
                            "property_types": " | ".join(profile["property_types"]),
                            "budget_currency": profile.get("budget_currency", "CLP"),
                            "budget_amount": profile.get("budget_amount", ""),
                            "max_price_clp": profile["max_price_clp"],
                            "query": profile.get("query", ""),
                            "listing_key": item["listing_key"],
                            "source_type": item.get("source_type", ""),
                            "source_portal": item.get("source_portal", ""),
                            "commune": item.get("commune", ""),
                            "property_type": item.get("property_type", ""),
                            "title": item.get("title", ""),
                            "description_excerpt": item.get("description_excerpt", ""),
                            "price_clp": item.get("price_clp", ""),
                            "price_uf": item.get("price_uf", ""),
                            "price_currency": item.get("price_currency", ""),
                            "attributes_json": json.dumps(item.get("attributes", {}), ensure_ascii=False),
                            "features_json": json.dumps(item.get("features", {}), ensure_ascii=False),
                            "review_url": item.get("url", ""),
                            "human_relevant": "", "human_hard_requirement_violation": "", "human_notes": "",
                        }
                    keys.append({
                        "profile_id": profile["profile_id"], "listing_key": item["listing_key"],
                        "variant": variant, "group": group, "rank": rank,
                        "score": item.get("score"), "bm25": item.get("bm25"),
                        "cosine": item.get("cosine"), "reranker_score": item.get("reranker_score"),
                        "review_reasons": json.dumps(item.get("review_reasons", []), ensure_ascii=False),
                    })
    ordered = list(candidates.items())
    random.Random(20261126).shuffle(ordered)
    candidate_ids = {key: f"C{n:05d}" for n, (key, _) in enumerate(ordered, 1)}
    blind_fields = ["candidate_id"] + list(ordered[0][1].keys()) if ordered else ["candidate_id"]
    blind_path = output_dir / "blind_review_candidates.csv"
    with blind_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=blind_fields)
        writer.writeheader()
        for key, row in ordered:
            writer.writerow({"candidate_id": candidate_ids[key], **row})
    key_path = output_dir / "unblind_ranking_key.csv"
    with key_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["candidate_id", *keys[0].keys()] if keys else ["candidate_id"])
        writer.writeheader()
        for row in keys:
            writer.writerow({"candidate_id": candidate_ids[(row["profile_id"], row["listing_key"])], **row})
    return blind_path, key_path, len(candidates)


def verify_resume(catalog_path, profiles_path, profiles, rows, as_of, output_dir):
    from chatbot.local_property_search import LocalPropertyIndex

    cli = ROOT / "scripts" / "run_local_property_search.py"
    output_path = output_dir / "resume_checkpoint.json"
    raw_catalog = Path(catalog_path).read_bytes()
    raw_profiles = Path(profiles_path).read_bytes()
    config = {
        "schema": 2,
        "inputs": [hashlib.sha256(raw_catalog).hexdigest(), hashlib.sha256(raw_profiles).hexdigest()],
        "model": None, "revision": None, "reranker": None, "reranker_revision": None,
        "identities": {}, "top": 10, "pool": 50, "device": "cpu",
        "batch_size": 16, "reranker_batch_size": 8,
    }
    index = LocalPropertyIndex(rows)
    checkpoint = {"config": config, "as_of": as_of.isoformat(), "status": "running", "results": []}
    for i, profile in enumerate(profiles[:2]):
        checkpoint["results"].append({
            "profile_id": profile.get("profile_id", i + 1),
            **index.search(profile, limit=10, now=as_of),
        })
    output_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [sys.executable, str(cli), "--catalog", str(catalog_path), "--profiles", str(profiles_path),
               "--output", str(output_path), "--resume", "--top", "10", "--device", "cpu"]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=600)
    if completed.returncode != 0:
        raise RuntimeError("CLI resume verification failed: " + completed.stderr[-1000:])
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    return {"status": saved.get("status"), "resumed_from_profiles": 2,
            "completed_profiles": len(saved.get("results", [])), "return_code": completed.returncode}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--hf-cache-dir", type=Path,
                        help="Optional local Hugging Face cache shared across repeat runs")
    parser.add_argument("--as-of", required=True, help="UTC timestamp of the exported snapshot")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--variants", nargs="+",
                        choices=("minilm_filters", "bm25", "bge_m3", "bge_m3_reranker"),
                        help="Run one or more variants; default runs all four in one process")
    parser.add_argument("--minilm-batch-size", type=int, default=16)
    parser.add_argument("--bge-batch-size", type=int, default=16)
    parser.add_argument("--reranker-batch-size", type=int, default=4)
    parser.add_argument("--rerank-pool", type=int, default=50)
    parser.add_argument("--skip-resume-check", action="store_true",
                        help="Skip the CLI checkpoint/resume smoke test for this partial run")
    parser.add_argument("--resume-partial", action="store_true",
                        help="Resume a terminated benchmark from its own matching model cache and progress file")
    args = parser.parse_args()
    if not args.catalog.is_file() or not args.profiles.is_file():
        parser.error("catalog and profiles must exist")
    if (not 1 <= args.minilm_batch_size <= 128 or not 1 <= args.bge_batch_size <= 32
            or not 1 <= args.reranker_batch_size <= 16):
        parser.error("batch sizes are outside the safe local range")
    args.output_dir.mkdir(parents=True, exist_ok=args.resume_partial)
    hf_home = args.hf_cache_dir or (args.output_dir / "hf-cache")
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(hf_home)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

    import numpy as np
    import psutil
    import torch
    import sentence_transformers
    from datetime import datetime
    from chatbot.local_property_search import LocalPropertyIndex

    rows = json.loads(args.catalog.read_text(encoding="utf-8"))
    profiles = json.loads(args.profiles.read_text(encoding="utf-8"))
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        parser.error("--as-of must include a UTC offset")
    run_identity = hashlib.sha256(json.dumps([
        sha256_file(args.catalog), sha256_file(args.profiles), as_of.isoformat(),
    ], separators=(",", ":")).encode("utf-8")).hexdigest()
    if len(profiles) < 30:
        parser.error("the comparison requires at least 30 profiles")
    requested_device = args.device
    if requested_device == "auto":
        requested_device = "cuda" if torch.cuda.is_available() else "cpu"
    if requested_device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA requested but unavailable in this virtual environment")

    variant_specs = [
        {"name": "minilm_filters", "embedding": MINILM,
         "embedding_batch_size": args.minilm_batch_size},
        {"name": "bm25", "embedding": None},
        {"name": "bge_m3", "embedding": BGE_M3,
         "embedding_batch_size": args.bge_batch_size},
        {"name": "bge_m3_reranker", "embedding": BGE_M3,
         "reranker": BGE_RERANKER, "embedding_batch_size": args.bge_batch_size,
         "reranker_batch_size": args.reranker_batch_size},
    ]
    requested_variants = args.variants or [spec["name"] for spec in variant_specs]
    by_name = {spec["name"]: spec for spec in variant_specs}
    variants = [by_name[name] for name in requested_variants]
    all_results = {}
    metrics = []
    for spec in variants:
        variant_metrics, variant_results = run_variant(
            spec, rows, profiles, as_of, args.output_dir, requested_device,
            args.rerank_pool, args.resume_partial, run_identity,
            torch, np, psutil, sentence_transformers)
        metrics.append(variant_metrics)
        all_results[spec["name"]] = variant_results
        (args.output_dir / f"{spec['name']}_results.json").write_text(
            json.dumps(variant_results, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    blind_path, key_path, blind_count = write_blind_review(profiles, all_results, args.output_dir)
    resume = ({"status": "skipped", "reason": "partial variant run"}
              if args.skip_resume_check else
              verify_resume(args.catalog, args.profiles, profiles, rows, as_of, args.output_dir))
    payload = {
        "snapshot_sha256": sha256_file(args.catalog), "profiles_sha256": sha256_file(args.profiles),
        "as_of": as_of.isoformat(), "catalog_rows": len(rows), "profile_count": len(profiles),
        "profile_operations": {op: sum(p["operation"] == op for p in profiles) for op in ("VENTA", "ARRIENDO")},
        "uf_budget_count": sum(p.get("budget_currency") == "UF" for p in profiles),
        "clp_budget_count": sum(p.get("budget_currency") == "CLP" for p in profiles),
        "uf_conversion": {"clp_per_uf": 40809.44, "date": "2026-11-26",
                          "source": "https://www.sii.cl/valores_y_fechas/uf/uf2026.htm"},
        "software": {"python": sys.version.split()[0], "torch": torch.__version__,
                     "cuda_runtime": torch.version.cuda, "sentence_transformers": sentence_transformers.__version__,
                     "cuda_available": torch.cuda.is_available(),
                     "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None},
        "code_base_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True, check=True).stdout.strip(),
        "implementation_sha256": {
            name: sha256_file(ROOT / name) for name in (
                "chatbot/local_property_search.py",
                "chatbot/local_embedding_cache.py",
                "scripts/run_local_property_search.py",
                "scripts/benchmark_local_property_search.py",
                "tests/fixtures/local_property_search_profiles.json",
            )
        },
        "variants": metrics,
        "relevance_metrics": {"precision_at_5": None, "recall_at_10": None, "ndcg_at_10": None,
                              "reason": "No independent human labels were supplied; use the blind review sheet."},
        "hard_requirement_violations_in_compatible": sum(m["hard_requirement_violations_in_compatible"] for m in metrics),
        "external_rows_in_compatible": sum(m["external_rows_in_compatible"] for m in metrics),
        "blind_review_candidates": blind_count,
        "blind_review_file": str(blind_path), "unblind_key_file": str(key_path),
        "resume_verification": resume,
    }
    metrics_path = args.output_dir / "comparison_metrics.json"
    metrics_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    lines = [
        "# Comparación local del motor de búsqueda",
        "",
        f"- Snapshot SHA-256: `{payload['snapshot_sha256']}`",
        f"- Filas: {len(rows):,}; perfiles sintéticos: {len(profiles)} ({payload['profile_operations']['VENTA']} ventas, {payload['profile_operations']['ARRIENDO']} arriendos)",
        f"- UF para presupuestos: CLP {40809.44:,.2f} al 2026-11-26 ([SII](https://www.sii.cl/valores_y_fechas/uf/uf2026.htm)).",
        "- Precision@5, Recall@10 y nDCG@10 quedan pendientes de etiquetas humanas independientes.",
        f"- Evaluación ciega: `{blind_path.name}`; clave de rankings: `{key_path.name}`.",
        f"- Reanudación CLI: `{resume.get('status')}`, {resume.get('completed_profiles', 0)} perfiles.",
        "",
        "| Variante | Lotes | Dispositivo | Índice frío (s) | Índice caliente (s) | Latencia p50 (ms) | p95 (ms) | consultas/s | RAM pico (MB) | VRAM reservada (MB) | violaciones duras |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for m in metrics:
        memory = m["memory_peaks"]
        lines.append("| {variant} | emb={batch}, rerank={rerank_batch} | {device}/{precision} | {cold:.3f} | {warm:.3f} | {p50:.2f} | {p95:.2f} | {qps:.3f} | {ram:.1f} | {vram:.1f} | {violations} |".format(
            variant=m["variant"], device=m["device"], precision=m["precision"],
            batch=m["embedding_batch_size"] or "—", rerank_batch=m["reranker_batch_size"] or "—",
            cold=m["index_cold_seconds"], warm=m["index_warm_seconds"],
            p50=m["query_latency_p50_ms"], p95=m["query_latency_p95_ms"],
            qps=m["query_throughput_per_second"], ram=memory["peak_rss_mb"] or 0,
            vram=memory["peak_gpu_reserved_mb"] or 0,
            violations=m["hard_requirement_violations_in_compatible"]))
    (args.output_dir / "comparison_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"metrics": str(metrics_path), "report": str(args.output_dir / 'comparison_report.md'),
                      "blind_review": str(blind_path), "variants": [m['variant'] for m in metrics],
                      "hard_violations": payload["hard_requirement_violations_in_compatible"],
                      "external_compatible": payload["external_rows_in_compatible"],
                      "blind_candidates": blind_count, "resume": resume}, ensure_ascii=False))


if __name__ == "__main__":
    main()
