"""Local Windows-compatible batch runner: JSON in, checkpointed JSON out, no Mongo writes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chatbot.local_property_search import LocalPropertyIndex, validate_profile
from chatbot.local_embedding_cache import EmbeddingCache, model_namespace

class LocalEmbeddings:
    def __init__(self, model, device, allow_download, revision=None, batch_size=16):
        from sentence_transformers import SentenceTransformer
        import torch
        model_kwargs = {"torch_dtype": torch.float16} if device == "cuda" else None
        self.model = SentenceTransformer(model, device=device, revision=revision,
                                         local_files_only=not allow_download,
                                         trust_remote_code=False, model_kwargs=model_kwargs)
        self.batch_size = batch_size
    def encode(self, texts):
        return self.model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True, show_progress_bar=False)

class LocalReranker:
    def __init__(self, model, device, allow_download, revision=None, batch_size=4):
        from sentence_transformers import CrossEncoder
        import torch
        model_kwargs = {"torch_dtype": torch.float16} if device == "cuda" else None
        self.model = CrossEncoder(model, device=device, revision=revision,
                                  local_files_only=not allow_download,
                                  trust_remote_code=False, model_kwargs=model_kwargs)
        self.batch_size = batch_size
    def score(self, query, texts):
        return self.model.predict([(query, text) for text in texts], batch_size=self.batch_size, show_progress_bar=False)

def write_checkpoint(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", required=True, type=Path)
    p.add_argument("--profiles", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--model")
    p.add_argument("--revision", help="Immutable embedding model revision")
    p.add_argument("--cache", type=Path)
    p.add_argument("--reranker")
    p.add_argument("--reranker-revision")
    p.add_argument("--rerank-pool", type=int, default=50)
    p.add_argument("--batch-size", type=int, default=16, help="Embedding batch size; lower it to fit GPU memory")
    p.add_argument("--reranker-batch-size", type=int, default=8, help="Cross-encoder batch size")
    p.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    p.add_argument("--allow-model-download", action="store_true")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()
    if not 1 <= args.top <= 500 or not 1 <= args.rerank_pool <= 500:
        p.error("top and rerank-pool must be 1..500")
    if not 1 <= args.batch_size <= 256 or not 1 <= args.reranker_batch_size <= 64:
        p.error("batch-size must be 1..256 and reranker-batch-size 1..64")
    if args.cache and not args.model:
        p.error("--cache requires --model")
    inputs = {args.catalog.resolve(), args.profiles.resolve()}
    targets = {args.output.resolve(), args.output.with_suffix(args.output.suffix+".tmp").resolve()}
    if args.cache:
        targets.add(args.cache.resolve())
        if len(targets) != 3:
            p.error("cache and output paths must differ")
    if inputs & targets:
        p.error("output/cache must not overwrite input")
    raw = [args.catalog.read_bytes(), args.profiles.read_bytes()]
    rows, profiles = [json.loads(x.decode("utf-8-sig")) for x in raw]
    if not isinstance(rows, list) or not isinstance(profiles, list):
        p.error("catalog and profiles must be JSON arrays")
    for profile in profiles:
        validate_profile(profile)
    model = None
    cache = None
    reranker = None
    identities = {}
    if args.model or args.reranker:
        import sentence_transformers
        version = sentence_transformers.__version__
        for name, identifier, revision in (("embedding", args.model, args.revision),
                                              ("reranker", args.reranker, args.reranker_revision)):
            if not identifier:
                continue
            try:
                identities[name] = model_namespace(identifier, revision, version=version)
            except ValueError:
                if args.resume or (name == "embedding" and args.cache):
                    raise
                identities[name] = "unpinned"
        if args.model:
            model = LocalEmbeddings(args.model, args.device, args.allow_model_download, args.revision, args.batch_size)
        if args.cache:
            cache = EmbeddingCache(args.cache, identities["embedding"])
        if args.reranker:
            reranker = LocalReranker(args.reranker, args.device, args.allow_model_download, args.reranker_revision,
                                     args.reranker_batch_size)
    config = {"schema":2, "inputs":[hashlib.sha256(x).hexdigest() for x in raw],
              "model":args.model,"revision":args.revision,"reranker":args.reranker,
              "reranker_revision":args.reranker_revision,"identities":identities,
              "top":args.top,"pool":args.rerank_pool,"device":args.device,
              "batch_size":args.batch_size,"reranker_batch_size":args.reranker_batch_size}
    checkpoint = {"config":config,"as_of":datetime.now(timezone.utc).isoformat(),
                  "status":"running","results":[]}
    if args.resume:
        if "unpinned" in identities.values():
            p.error("resume requires immutable model identity")
        checkpoint = json.loads(args.output.read_text(encoding="utf-8"))
        if checkpoint["config"] != config:
            p.error("checkpoint inputs/config changed; start a fresh output")
        if len(checkpoint["results"]) > len(profiles):
            p.error("invalid checkpoint length")
        for i, result in enumerate(checkpoint["results"]):
            if result.get("profile_id") != profiles[i].get("profile_id", i+1):
                p.error("checkpoint profile order mismatch")
    elif args.output.exists():
        p.error("output exists; choose another file or use --resume")
    now = datetime.fromisoformat(checkpoint["as_of"])
    write_checkpoint(args.output, checkpoint)
    index = LocalPropertyIndex(rows, model, cache=cache, reranker=reranker, rerank_pool=args.rerank_pool)
    for i in range(len(checkpoint["results"]), len(profiles)):
        profile = profiles[i]
        checkpoint["results"].append({"profile_id":profile.get("profile_id", i+1), **index.search(profile,args.top,now=now)})
        write_checkpoint(args.output, checkpoint)
        print(f"{i+1}/{len(profiles)} profiles processed", flush=True)
    checkpoint["status"] = "completed"
    write_checkpoint(args.output, checkpoint)

if __name__ == "__main__":
    main()
