"""Git worktree isolation and diff capture helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run_git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )


def is_git_repo(cwd: str | Path) -> bool:
    proc = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def repo_root(cwd: str | Path) -> Path | None:
    proc = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def create_worktree(
    source_cwd: str | Path,
    task_id: str,
    runs_dir: Path,
    base_ref: str | None = None,
) -> tuple[Path, str]:
    """Create an isolated git worktree + branch for parallel agent execution."""
    root = repo_root(source_cwd)
    if root is None:
        raise RuntimeError(
            f"Cannot create git worktree: '{source_cwd}' is not inside a Git repository."
        )

    worktree_dir = (runs_dir / task_id / "worktree").resolve()
    branch_name = f"agentctl/{task_id}"
    args = ["worktree", "add", "-b", branch_name, str(worktree_dir)]
    if base_ref:
        args.append(base_ref)

    proc = _run_git(args, root)
    if proc.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return worktree_dir, branch_name


def capture_git_diff(cwd: str | Path) -> str | None:
    """Capture status + diff of tracked modifications in a git repo."""
    if not is_git_repo(cwd):
        return None

    status = _run_git(["status", "--short"], cwd).stdout.strip()
    diff = _run_git(["diff", "HEAD"], cwd)
    if diff.returncode != 0:
        # Repo might not have an initial commit yet
        diff = _run_git(["diff"], cwd)

    parts: list[str] = []
    if status:
        parts.append("# git status --short\n" + status + "\n")
    if diff.stdout.strip():
        parts.append("# git diff\n" + diff.stdout)
    return "\n".join(parts) if parts else ""
