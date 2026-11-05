from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DATA = ROOT / "data"
DEPRECATED = DATA / "_deprecated_unused"
ACTIVE_JSONS = [
    DATA / "known_broker_brands.json",
    DATA / "hard_broker_terms.json",
    DATA / "company_shape_terms.json",
    DATA / "listing_removed_patterns.json",
    DATA / "search_terms.json",
    DATA / "owner_keywords.json",
    DATA / "contextual_classification.json",
]
OBSOLETE_JSONS = [
    "known_companies.json",
    "broker_keywords.json",
    "broker_companies_chile.json",
]
ACTIVE_FILES = [
    ROOT / "run_owner_hunt.py",
    ROOT / "classifier_rules.py",
    ROOT / "deepseek_classifier.py",
    ROOT / "downloader.py",
    ROOT / "discovery.py",
    ROOT / "extractor.py",
    ROOT / "export_reports.py",
    ROOT / "mongo_store.py",
    ROOT / "config.py",
    ROOT / "proxy_manager.py",
    ROOT / "README.md",
]
PROHIBITED_NAMES = [
    "LISTING_REMOVED_PATTERNS",
    "CORREDOR_PATTERNS",
    "OWNERSHIP_PATTERNS",
    "DEFAULT_OWNER_SEARCH_TERMS",
    "BROKER_COMPANIES",
    "KNOWN_COMPANIES",
]
LEGACY_IMPORT_PATTERNS = [
    r"\bscraping_yapo_proxys\b",
    r"from\s+scraping_yapo_proxys\s+import",
    r"import\s+scraping_yapo_proxys",
]


def _line_for(text: str, pattern: str) -> tuple[int, str] | None:
    for idx, line in enumerate(text.splitlines(), start=1):
        if pattern in line:
            return idx, line.rstrip()
    return None


def _regex_line_for(text: str, pattern: str) -> tuple[int, str] | None:
    rx = re.compile(pattern)
    for idx, line in enumerate(text.splitlines(), start=1):
        if rx.search(line):
            return idx, line.rstrip()
    return None


def _fail(msg: str) -> None:
    print(f"ERROR - {msg}")
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"OK - {msg}")


def main() -> int:
    if not DEPRECATED.exists():
        _fail("data/_deprecated_unused/ no existe")

    missing = [path for path in ACTIVE_JSONS if not path.exists()]
    if missing:
        _fail("Faltan JSON activos: " + ", ".join(str(path.relative_to(ROOT)) for path in missing))
    _ok("JSON activos encontrados")

    obsoletes_in_root = []
    for name in OBSOLETE_JSONS:
        if (DATA / name).exists():
            obsoletes_in_root.append(DATA / name)
    if obsoletes_in_root:
        _fail("JSON obsoletos aún están en data raíz: " + ", ".join(str(path.relative_to(ROOT)) for path in obsoletes_in_root))

    obsoletes_missing = []
    for name in OBSOLETE_JSONS:
        if (DATA / name).exists():
            continue
        if not (DEPRECATED / name).exists() and name != "broker_companies_chile.json":
            obsoletes_missing.append(name)
    if obsoletes_missing:
        _fail("JSON obsoletos no están archivados: " + ", ".join(obsoletes_missing))
    _ok("JSON obsoletos fuera de data raíz")

    issues: list[str] = []
    for path in ACTIVE_FILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in LEGACY_IMPORT_PATTERNS:
            found = _regex_line_for(text, pattern)
            if found:
                issues.append(f"{path.name}:{found[0]} legacy import -> {found[1]}")
        for token in PROHIBITED_NAMES:
            found = _line_for(text, token)
            if found:
                issues.append(f"{path.name}:{found[0]} prohibited constant -> {found[1]}")
        # Deterministic signal vocabularies live in data JSON. The legacy
        # DeepSeek prompt may discuss examples in prose, but it is not a rule
        # source and must not cause this static data-boundary check to fail.
        if path.name in {"run_owner_hunt.py", "classifier_rules.py", "extractor.py"}:
            for token in [
                "dueño vende",
                "trato directo",
                "sin comisión",
                "corredor de propiedades",
                "comisión de corretaje",
                "anuncio eliminado",
                "listing removed",
            ]:
                found = _line_for(text, token)
                if found:
                    issues.append(f"{path.name}:{found[0]} hardcoded classification term -> {found[1]}")

    if issues:
        print("ERROR - hardcoded terms/imports found in active files")
        for issue in issues:
            print(issue)
        raise SystemExit(1)
    _ok("sin imports a scraping_yapo_proxys.py")
    _ok("sin constantes inmobiliarias hardcodeadas en módulos activos")

    rule_sets = []
    try:
        try:
            from .classifier_rules import load_rule_sets
        except ImportError:
            from scrapers.scraper_yapo.classifier_rules import load_rule_sets

        rule_sets = list(load_rule_sets().keys())
    except Exception as exc:
        _fail(f"No se pudo cargar classifier_rules.load_rule_sets(): {exc}")

    expected_keys = {
        "known_broker_brand_aliases",
        "known_broker_brands",
        "hard_broker_terms",
        "company_shape_terms",
        "listing_removed_patterns",
        "search_terms",
        "owner_keywords",
        "contextual_classification",
    }
    if set(rule_sets) != expected_keys:
        _fail(f"load_rule_sets() no carga exactamente los JSON activos. Encontrado: {sorted(rule_sets)}")

    import json
    brands = json.loads((DATA / "known_broker_brands.json").read_text(encoding="utf-8-sig"))
    aliases = brands.get("brand_aliases", {}) if isinstance(brands, dict) else {}
    if not isinstance(aliases, dict) or any(
        brand not in brands.get("brands", []) or not isinstance(values, list) or not values
        for brand, values in aliases.items()
    ):
        _fail("known_broker_brands.json contiene alias inválidos o huérfanos")
    if not isinstance(load_rule_sets().get("contextual_classification"), dict):
        _fail("contextual_classification.json no se cargó como reglas estructuradas")
    _ok("alias del catálogo y reglas contextuales cargados")

    py_files = [
        "config.py",
        "proxy_manager.py",
        "downloader.py",
        "discovery.py",
        "extractor.py",
        "classifier_rules.py",
        "deepseek_classifier.py",
        "mongo_store.py",
        "export_reports.py",
        "run_owner_hunt.py",
        "pilot.py",
        "audit_contextual_filter.py",
        "verify_clean_owner_hunt.py",
    ]
    cmd = [sys.executable, "-m", "py_compile", *py_files]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        _fail("py_compile falló")
    _ok("py_compile pasó")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
