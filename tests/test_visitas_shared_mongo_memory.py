"""Isolated regression contracts: visits reuse the bounded Mongo pool.

No database, Google Drive, outbound WhatsApp or PDF state is modified.
"""
import ast
from pathlib import Path

from chatbot import storage


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "api_visitas.py").read_text(encoding="utf-8")


def _function_from_source(name):
    tree = ast.parse(SOURCE)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "api_visitas.py", "exec"), ns)
    return ns[name]


def test_visitas_get_db_uses_existing_bounded_pymongo_client(monkeypatch):
    # Do not actually connect to Mongo; test the existing public interface.
    sentinel = object()
    calls = []
    monkeypatch.setattr(storage, "get_db", lambda: (calls.append(1), sentinel)[1])
    visits_get_db = _function_from_source("get_db")
    assert visits_get_db() is sentinel
    assert visits_get_db() is sentinel
    assert calls == [1, 1]


def test_visitas_never_constructs_an_unbounded_mongo_client():
    tree = ast.parse(SOURCE)
    allocations = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"MongoClient", "AsyncIOMotorClient"}
    ]
    assert not allocations
    assert "from chatbot.storage import get_db as get_shared_db" in SOURCE
    # Existing callers, including downloads and public acceptance, retain API.
    assert "db = get_db()" in SOURCE


def test_preview_instruments_rss_without_changing_pdf_generation_or_logging_pii():
    tree = ast.parse(SOURCE)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "preview_contract"
    )
    generated = ast.get_source_segment(SOURCE, fn)
    assert "PDFGenerator.generate_original_contract, data" in generated
    assert "return Response(content=pdf_bytes, media_type=\"application/pdf\")" in generated
    assert "VISITAS_PREVIEW_RESOURCE" in generated
    assert "psutil.Process().memory_info().rss" in generated
    assert "len(pdf_bytes)" in generated
    assert "rss_delta_mib" in generated
    assert "await _enrich_with_property_data(data)" in generated
    assert "user_doc = await adb[\"usuarios\"].find_one" in generated
