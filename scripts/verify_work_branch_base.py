#!/usr/bin/env python3
"""Fail-closed pre-work check for branches based on current production main."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PRODUCTION_DEPLOY_SOURCE = "main"
DEFAULT_MAIN_REF = "origin/main"
DEFAULT_MAX_BEHIND = 0
# This is the merge-base identified by the production incident audit. It is
# deliberately explicit so a stale historical branch cannot pass by name only.
KNOWN_DANGEROUS_MERGE_BASES = frozenset({
    "2e83aecc760086b00628d56d149697780a8eefd0",
})
EXIT_OK = 0
EXIT_STALE = 1
EXIT_ERROR = 2


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=False,
    )


def _resolve_commit(repo: Path, ref: str) -> str | None:
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _repository_root(repo_arg: str | None) -> Path:
    if repo_arg:
        return Path(repo_arg).expanduser().resolve()
    result = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("not a Git repository")
    return Path(result.stdout.strip()).resolve()


def _current_branch(repo: Path) -> str | None:
    result = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _fetch_main(repo: Path) -> bool:
    result = _git(repo, "fetch", "--no-tags", "origin", "main")
    return result.returncode == 0


def _behind_ahead(repo: Path, main_commit: str, head_commit: str) -> tuple[int, int] | None:
    result = _git(repo, "rev-list", "--left-right", "--count", f"{main_commit}...{head_commit}")
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    if len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


def verify_work_branch_base(
    *,
    repo: Path,
    main_ref: str = DEFAULT_MAIN_REF,
    head_ref: str = "HEAD",
    max_behind: int = DEFAULT_MAX_BEHIND,
    fetch: bool = True,
    dangerous_merge_bases: frozenset[str] = KNOWN_DANGEROUS_MERGE_BASES,
) -> int:
    """Return a process status for a safe, current-main feature base."""
    if fetch and not _fetch_main(repo):
        print("WORK_BRANCH_BASE_STALE origin/main_fetch_failed", file=sys.stderr)
        return EXIT_STALE

    branch = _current_branch(repo)
    main_commit = _resolve_commit(repo, main_ref)
    head_commit = _resolve_commit(repo, head_ref)
    if not branch or not main_commit or not head_commit:
        print(
            "WORK_BRANCH_BASE_STALE "
            f"branch={branch or 'detached'} main_ref={main_ref}",
            file=sys.stderr,
        )
        return EXIT_STALE

    merge_base_result = _git(repo, "merge-base", main_commit, head_commit)
    merge_base = merge_base_result.stdout.strip() if merge_base_result.returncode == 0 else ""
    if not merge_base:
        print("WORK_BRANCH_BASE_STALE merge_base_unavailable", file=sys.stderr)
        return EXIT_STALE
    if merge_base in dangerous_merge_bases:
        print(
            "WORK_BRANCH_BASE_STALE "
            f"branch={branch} reason=dangerous_merge_base merge_base={merge_base}",
            file=sys.stderr,
        )
        return EXIT_STALE

    ancestry = _git(repo, "merge-base", "--is-ancestor", main_commit, head_commit)
    if ancestry.returncode != 0:
        print(
            "WORK_BRANCH_BASE_STALE "
            f"branch={branch} reason=main_not_ancestor main={main_commit} head={head_commit}",
            file=sys.stderr,
        )
        return EXIT_STALE

    counts = _behind_ahead(repo, main_commit, head_commit)
    if counts is None:
        print("WORK_BRANCH_BASE_STALE branch_distance_unavailable", file=sys.stderr)
        return EXIT_STALE
    behind, ahead = counts
    if behind > max_behind:
        print(
            "WORK_BRANCH_BASE_STALE "
            f"branch={branch} behind={behind} ahead={ahead} max_behind={max_behind}",
            file=sys.stderr,
        )
        return EXIT_STALE

    print(
        "WORK_BRANCH_BASE_OK "
        f"source={PRODUCTION_DEPLOY_SOURCE} branch={branch} "
        f"main={main_commit} head={head_commit} behind={behind} ahead={ahead}"
    )
    return EXIT_OK


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-ref", default=DEFAULT_MAIN_REF)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo")
    parser.add_argument("--max-behind", type=int, default=DEFAULT_MAX_BEHIND)
    parser.add_argument("--no-fetch", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.max_behind < 0:
        print("WORK_BRANCH_BASE_STALE invalid_max_behind", file=sys.stderr)
        return EXIT_STALE
    try:
        repo = _repository_root(args.repo)
    except RuntimeError as exc:
        print(f"WORK_BRANCH_BASE_STALE {exc}", file=sys.stderr)
        return EXIT_STALE
    return verify_work_branch_base(
        repo=repo,
        main_ref=args.main_ref,
        head_ref=args.head,
        max_behind=args.max_behind,
        fetch=not args.no_fetch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
