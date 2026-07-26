import re
import subprocess
from pathlib import Path


SECRET_PATTERNS = (
    re.compile(r"mongodb(?:\+srv)?://[^/\s:@]+:[^@\s]+@", re.I),
    re.compile(r"(?:wasender[_-]?(?:token|key)|api[_-]?key|secret)\s*=\s*['\"][^'\"]{12,}", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def test_tracked_files_do_not_contain_embedded_credentials():
    root = Path(__file__).parents[1]
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root
    ).decode("utf-8").split("\0")
    findings = []
    for relative in filter(None, tracked):
        path = root / relative
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            findings.append(relative)
    assert findings == [], f"Potential embedded credentials in: {findings}"


def test_migration_scripts_require_mongo_uri():
    root = Path(__file__).parents[1]
    for relative in (
        "scripts/resolve_hot_notification_duplicates.py",
        "scripts/detect_leads_without_cycles.py",
        "scripts/phase0_prep.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "if not Config.MONGO_URI:" in source
        assert "local fallback" in source

