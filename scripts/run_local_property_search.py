"""Local Windows-compatible batch runner: JSON in, JSON out, no Mongo writes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chatbot.local_property_search import LocalPropertyIndex

class LocalEmbeddings:
    def __init__(self, model, device, allow_download):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model, device=device, local_files_only=not allow_download, trust_remote_code=False)
    def encode(self, texts):
        return self.model.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=True)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", required=True, type=Path)
    p.add_argument("--profiles", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--model", help="Optional local multilingual embedding model or model directory")
    p.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    p.add_argument("--allow-model-download", action="store_true")
    p.add_argument("--top", type=int, default=10)
    args = p.parse_args()
    rows = json.loads(args.catalog.read_text(encoding="utf-8-sig"))
    profiles = json.loads(args.profiles.read_text(encoding="utf-8-sig"))
    if not isinstance(rows, list) or not isinstance(profiles, list):
        p.error("catalog and profiles must be JSON arrays")
    if args.output.resolve() in {args.catalog.resolve(), args.profiles.resolve()}:
        p.error("output must not overwrite input")
    model = LocalEmbeddings(args.model, args.device, args.allow_model_download) if args.model else None
    index = LocalPropertyIndex(rows, model)
    output = []
    for i, profile in enumerate(profiles, 1):
        output.append({"profile_id": profile.get("profile_id", i), **index.search(profile, args.top)})
        print(f"{i}/{len(profiles)} profiles processed", flush=True)
    temp = args.output.with_suffix(args.output.suffix + ".tmp")
    temp.write_text(json.dumps({"model": args.model, "results": output}, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(args.output)

if __name__ == "__main__":
    main()
