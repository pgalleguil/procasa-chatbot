"""Merge four independently completed local property-search benchmark runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.benchmark_local_property_search import write_blind_review

ORDER = ("minilm_filters", "bm25", "bge_m3", "bge_m3_reranker")


def read_run(path, expected_variant):
    payload_path = path / "comparison_metrics.json"
    if not payload_path.is_file():
        raise ValueError(f"missing completed metrics: {payload_path}")
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    variants = payload.get("variants") or []
    if len(variants) != 1 or variants[0].get("variant") != expected_variant:
        raise ValueError(f"{path} must contain exactly {expected_variant}")
    result_path = path / f"{expected_variant}_results.json"
    if not result_path.is_file():
        raise ValueError(f"missing ranked results: {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    return payload, variants[0], result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ORDER:
        parser.add_argument("--" + name.replace("_", "-"), type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.profiles.is_file():
        parser.error("profiles file must exist")
    args.output_dir.mkdir(parents=True, exist_ok=False)

    profiles = json.loads(args.profiles.read_text(encoding="utf-8"))
    payloads = {}
    metrics = {}
    all_results = {}
    for name in ORDER:
        payload, metric, results = read_run(getattr(args, name), name)
        payloads[name] = payload
        metrics[name] = metric
        all_results[name] = results

    first = payloads[ORDER[0]]
    stable_source_files = (
        "chatbot/local_property_search.py",
        "chatbot/local_embedding_cache.py",
        "scripts/run_local_property_search.py",
        "tests/fixtures/local_property_search_profiles.json",
    )
    for name in ORDER[1:]:
        candidate = payloads[name]
        for field in ("snapshot_sha256", "profiles_sha256", "as_of", "catalog_rows",
                      "profile_count", "software"):
            if candidate.get(field) != first.get(field):
                raise ValueError(f"runs differ in {field}: {ORDER[0]} vs {name}")
        for source_file in stable_source_files:
            if candidate["implementation_sha256"].get(source_file) != first["implementation_sha256"].get(source_file):
                raise ValueError(f"engine/profile source differs in {source_file}: {ORDER[0]} vs {name}")
    if first["profiles_sha256"] != __import__("hashlib").sha256(args.profiles.read_bytes()).hexdigest():
        raise ValueError("aggregate profiles do not match benchmark runs")

    blind_path, key_path, blind_count = write_blind_review(profiles, all_results, args.output_dir)
    resume = payloads["bm25"].get("resume_verification", {"status": "not_run"})
    combined = {
        "snapshot_sha256": first["snapshot_sha256"],
        "profiles_sha256": first["profiles_sha256"],
        "as_of": first["as_of"],
        "catalog_rows": first["catalog_rows"],
        "profile_count": first["profile_count"],
        "profile_operations": first["profile_operations"],
        "uf_budget_count": first["uf_budget_count"],
        "clp_budget_count": first["clp_budget_count"],
        "uf_conversion": first["uf_conversion"],
        "software": first["software"],
        "variant_code_base_commits": {name: payloads[name]["code_base_commit"] for name in ORDER},
        "engine_and_profile_sha256": {
            source_file: first["implementation_sha256"][source_file]
            for source_file in stable_source_files
        },
        "variant_harness_sha256": {
            name: payloads[name]["implementation_sha256"].get("scripts/benchmark_local_property_search.py")
            for name in ORDER
        },
        "variants": [metrics[name] for name in ORDER],
        "relevance_metrics": {
            "precision_at_5": None, "recall_at_10": None, "ndcg_at_10": None,
            "reason": "No independent human labels were supplied; use the blind review sheet.",
        },
        "hard_requirement_violations_in_compatible": sum(
            metric["hard_requirement_violations_in_compatible"] for metric in metrics.values()),
        "external_rows_in_compatible": sum(
            metric["external_rows_in_compatible"] for metric in metrics.values()),
        "blind_review_candidates": blind_count,
        "blind_review_file": str(blind_path),
        "unblind_key_file": str(key_path),
        "resume_verification": resume,
        "variant_run_directories": {name: str(getattr(args, name)) for name in ORDER},
    }
    metrics_path = args.output_dir / "comparison_metrics.json"
    metrics_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    lines = [
        "# Comparación local del motor de búsqueda",
        "",
        f"- Snapshot SHA-256: `{combined['snapshot_sha256']}`",
        f"- Filas: {combined['catalog_rows']:,}; perfiles sintéticos: {combined['profile_count']} "
        f"({combined['profile_operations']['VENTA']} ventas, {combined['profile_operations']['ARRIENDO']} arriendos)",
        f"- UF para presupuestos: CLP {combined['uf_conversion']['clp_per_uf']:,.2f} al {combined['uf_conversion']['date']} "
        f"([SII]({combined['uf_conversion']['source']})).",
        "- Precision@5, Recall@10 y nDCG@10 quedan pendientes de etiquetas humanas independientes.",
        f"- Evaluación ciega: `{blind_path.name}`; clave de rankings: `{key_path.name}`.",
        f"- Reanudación CLI: `{resume.get('status')}`, {resume.get('completed_profiles', 0)} perfiles.",
        "",
        "| Variante | Dispositivo | Lotes (emb/rerank) | Índice frío (s) | Índice caliente (s) | Latencia p50 (ms) | p95 (ms) | consultas/s | RAM pico (MB) | VRAM reservada (MB) | violaciones duras |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ORDER:
        metric = metrics[name]
        memory = metric["memory_peaks"]
        batches = f"{metric['embedding_batch_size'] or '—'}/{metric['reranker_batch_size'] or '—'}"
        lines.append(
            f"| {name} | {metric['device']}/{metric['precision']} | {batches} | "
            f"{metric['index_cold_seconds']:.3f} | {metric['index_warm_seconds']:.3f} | "
            f"{metric['query_latency_p50_ms']:.2f} | {metric['query_latency_p95_ms']:.2f} | "
            f"{metric['query_throughput_per_second']:.3f} | "
            f"{memory['peak_rss_mb'] or 0:.1f} | {memory['peak_gpu_reserved_mb'] or 0:.1f} | "
            f"{metric['hard_requirement_violations_in_compatible']} |"
        )
    report_path = args.output_dir / "comparison_report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "metrics": str(metrics_path), "report": str(report_path),
        "blind_review": str(blind_path), "variants": list(ORDER),
        "blind_candidates": blind_count, "resume": resume,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
