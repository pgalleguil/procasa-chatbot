#!/usr/bin/env python3
"""Fail-closed ancestry guard for production deployments.

Production deploys are sourced from ``main``.  The guard deliberately checks
Git ancestry rather than dates, branch names, or commit messages: the
approved main base must be an ancestor of the commit being deployed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PRODUCTION_DEPLOY_SOURCE = "main"
EXIT_OK = 0
EXIT_DIVERGED = 1
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
    commit = result.stdout.strip()
    return commit or None


def _repository_root(repo_arg: str | None) -> Path:
    if repo_arg:
        return Path(repo_arg).expanduser().resolve()
    result = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("not a Git repository")
    return Path(result.stdout.strip()).resolve()


def verify_production_deploy_base(
    *,
    repo: Path,
    approved_main_ref: str = "origin/main",
    head_ref: str = "HEAD",
) -> int:
    """Return a process status suitable for a pre-build deployment gate."""
    approved_main = _resolve_commit(repo, approved_main_ref)
    head = _resolve_commit(repo, head_ref)
    if not approved_main or not head:
        print(
            "DEPLOY_BASE_ERROR "
            f"approved_main_ref={approved_main_ref} head_ref={head_ref}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    result = _git(repo, "merge-base", "--is-ancestor", approved_main, head)
    if result.returncode == 0:
        print(
            "DEPLOY_BASE_OK "
            f"source={PRODUCTION_DEPLOY_SOURCE} "
            f"approved_main={approved_main} head={head}"
        )
        return EXIT_OK
    if result.returncode == 1:
        print(
            "DEPLOY_BASE_DIVERGED "
            f"source={PRODUCTION_DEPLOY_SOURCE} "
            f"approved_main={approved_main} head={head}"
        )
        return EXIT_DIVERGED

    detail = result.stderr.strip().replace("\r", " ").replace("\n", " ")
    print(f"DEPLOY_BASE_ERROR {detail or 'merge-base check failed'}", file=sys.stderr)
    return EXIT_ERROR


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--approved-main-ref",
        default="origin/main",
        help="Git ref representing the approved current main base (default: origin/main)",
    )
    parser.add_argument(
        "--head",
        default="HEAD",
        help="Git ref or commit being deployed (default: HEAD)",
    )
    parser.add_argument(
        "--repo",
        help="Repository path; defaults to the current repository",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        repo = _repository_root(args.repo)
    except RuntimeError as exc:
        print(f"DEPLOY_BASE_ERROR {exc}", file=sys.stderr)
        return EXIT_ERROR
    return verify_production_deploy_base(
        repo=repo,
        approved_main_ref=args.approved_main_ref,
        head_ref=args.head,
    )


if __name__ == "__main__":
    raise SystemExit(main())
