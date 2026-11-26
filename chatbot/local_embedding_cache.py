"""Incremental, version-isolated embedding cache for the local search only."""
import hashlib
import json
import sqlite3
from pathlib import Path

class EmbeddingCache:
    def __init__(self, path, namespace, batch_size=32):
        if not namespace or not 1 <= batch_size <= 512:
            raise ValueError("invalid cache configuration")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.batch_size = batch_size

    def load_or_encode(self, keys, texts, embedder):
        import numpy as np
        if len(keys) != len(texts):
            raise ValueError("keys and texts differ")
        hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
        # Cache by text content, not listing id: unchanged duplicate text can reuse vectors.
        vectors = {}
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("CREATE TABLE IF NOT EXISTS vectors (namespace TEXT, digest TEXT, dim INTEGER, vector BLOB, PRIMARY KEY(namespace,digest))")
            for digest in set(hashes):
                row = db.execute("SELECT dim,vector FROM vectors WHERE namespace=? AND digest=?",
                                 (self.namespace, digest)).fetchone()
                if row:
                    vector = np.frombuffer(row[1], dtype="<f4").copy()
                    if row[0] == vector.size and self._valid(vector, np):
                        vectors[digest] = vector
            missing = {}
            for digest, text in zip(hashes, texts):
                if digest not in vectors:
                    missing[digest] = text
            items = list(missing.items())
            for offset in range(0, len(items), self.batch_size):
                batch = items[offset:offset+self.batch_size]
                matrix = np.asarray(embedder.encode([t for _, t in batch]), dtype="<f4")
                if matrix.ndim != 2 or len(matrix) != len(batch):
                    raise ValueError("invalid embedding matrix")
                for (digest, _), vector in zip(batch, matrix):
                    if not self._valid(vector, np):
                        raise ValueError("invalid normalized embedding")
                    vectors[digest] = vector
                    db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
                               (self.namespace, digest, vector.size, vector.tobytes()))
                db.commit()  # retain completed batches after interruption
            if not hashes:
                return np.empty((0, 0), dtype=np.float32)
            if len({vectors[d].size for d in hashes}) != 1:
                raise ValueError("embedding dimensions differ; use a new model namespace")
            return np.stack([vectors[d] for d in hashes])

    @staticmethod
    def _valid(vector, np):
        return (vector.ndim == 1 and vector.size > 0 and np.isfinite(vector).all()
                and abs(float(np.linalg.norm(vector)) - 1) < 0.01)

def model_namespace(model, revision, *, version):
    # Remote model revisions must be immutable commit hashes, not branch names.
    import re
    path = Path(model)
    if path.is_dir():
        digest = hashlib.sha256()
        for file in sorted(p for p in path.rglob("*") if p.is_file()):
            digest.update(str(file.relative_to(path)).encode())
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b""):
                    digest.update(chunk)
        identity = str(path.resolve()) + ":" + digest.hexdigest()
    elif isinstance(revision, str) and re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        identity = model + ":" + revision.lower()
    else:
        raise ValueError("persistent cache requires a local model directory or immutable --revision SHA")
    return hashlib.sha256(json.dumps([identity, version, "unit-float32-v1"]).encode()).hexdigest()
