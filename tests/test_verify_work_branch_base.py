from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.verify_work_branch_base import verify_work_branch_base


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "branch", "-m", "main")
    git(repo, "branch", "feature")
    git(repo, "update-ref", "refs/remotes/origin/main", "refs/heads/main")
    return repo


def test_current_branch_from_main_passes_without_fetch(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "switch", "feature")
    assert verify_work_branch_base(repo=repo, fetch=False) == 0


def test_branch_behind_current_main_is_stale(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "switch", "main")
    (repo / "file.txt").write_text("main update\n", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", "main update")
    git(repo, "update-ref", "refs/remotes/origin/main", "refs/heads/main")
    git(repo, "switch", "feature")
    assert verify_work_branch_base(repo=repo, fetch=False) == 1


def test_diverged_historical_merge_base_is_stale(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "switch", "main")
    (repo / "file.txt").write_text("main update\n", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", "main update")
    git(repo, "switch", "feature")
    (repo / "file.txt").write_text("feature update\n", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", "feature update")
    base = git(repo, "merge-base", "main", "feature")
    assert verify_work_branch_base(
        repo=repo, fetch=False, dangerous_merge_bases=frozenset({base})
    ) == 1


def test_detached_head_is_stale(tmp_path):
    repo = make_repo(tmp_path)
    commit = git(repo, "rev-parse", "main")
    git(repo, "checkout", "--detach", commit)
    assert verify_work_branch_base(repo=repo, fetch=False) == 1
