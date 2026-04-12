"""Checkpoint the mirror database to a dedicated git branch.

Saves the DB to a `memex-data` orphan branch. Only commits if the
DB content has changed. Prunes history to keep at most N checkpoints.

Usage:
  python action/checkpoint.py --db mirror.db --repo-dir .
  python action/checkpoint.py --db mirror.db --repo-dir . --max 5 --branch memex-data
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def run(cmd: list[str], cwd: str, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a git command with utf-8 encoding."""
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True,
        encoding="utf-8", errors="replace", check=check, **kwargs,
    )


def file_sha256(path: str) -> str:
    """SHA256 of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def checkpoint(
    db_path: str,
    repo_dir: str,
    branch: str = "memex-data",
    max_checkpoints: int = 5,
) -> bool:
    """Checkpoint the DB to an orphan branch. Returns True if a new checkpoint was created."""
    db_path = os.path.abspath(db_path)
    if not os.path.isfile(db_path):
        print(f"ERROR: {db_path} does not exist")
        return False

    db_size = os.path.getsize(db_path)
    print(f"Checkpointing {db_path} ({db_size / 1024 / 1024:.1f} MB) to branch '{branch}'")

    # Fetch the branch (might not exist yet)
    run(["git", "fetch", "origin", branch], cwd=repo_dir, check=False)

    # Create a temp worktree
    worktree = tempfile.mkdtemp(prefix="memex-checkpoint-")
    created_worktree = False

    try:
        # Try checking out existing branch into worktree
        result = run(
            ["git", "worktree", "add", worktree, f"origin/{branch}"],
            cwd=repo_dir, check=False,
        )

        if result.returncode == 0:
            # Branch exists, check it out properly
            run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=worktree, check=False)
            created_worktree = True
        else:
            # Branch doesn't exist — create orphan
            shutil.rmtree(worktree, ignore_errors=True)
            worktree = tempfile.mkdtemp(prefix="memex-checkpoint-")
            run(["git", "worktree", "add", "--detach", worktree], cwd=repo_dir)
            created_worktree = True
            run(["git", "checkout", "--orphan", branch], cwd=worktree)
            # Remove any files from the index
            run(["git", "rm", "-rf", "."], cwd=worktree, check=False)
            print(f"  Created new orphan branch '{branch}'")

        # Check if DB has changed
        existing_db = os.path.join(worktree, "mirror.db")
        if os.path.isfile(existing_db):
            old_hash = file_sha256(existing_db)
            new_hash = file_sha256(db_path)
            if old_hash == new_hash:
                print("  No changes detected, skipping checkpoint")
                return False
            print(f"  DB changed (old={old_hash[:12]}... new={new_hash[:12]}...)")
        else:
            print("  First checkpoint for this branch")

        # Copy DB to worktree
        shutil.copy2(db_path, existing_db)

        # Commit
        run(["git", "add", "mirror.db"], cwd=worktree)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        run(
            ["git", "commit", "-m", f"checkpoint {timestamp}"],
            cwd=worktree,
        )
        print(f"  Committed checkpoint: {timestamp}")

        # Push
        result = run(
            ["git", "push", "origin", f"HEAD:{branch}"],
            cwd=worktree, check=False,
        )
        if result.returncode != 0:
            # Might need force push if we pruned
            run(["git", "push", "origin", f"HEAD:{branch}", "--force"], cwd=worktree)
        print("  Pushed to origin")

        # Prune if too many checkpoints
        result = run(["git", "rev-list", "--count", "HEAD"], cwd=worktree)
        commit_count = int(result.stdout.strip())
        print(f"  Checkpoints on branch: {commit_count}")

        if commit_count > max_checkpoints:
            print(f"  Pruning to {max_checkpoints} checkpoints...")
            _prune_checkpoints(worktree, branch, max_checkpoints)
            run(["git", "push", "origin", f"HEAD:{branch}", "--force"], cwd=worktree)
            print(f"  Pruned and force-pushed")

        return True

    finally:
        if created_worktree:
            run(["git", "worktree", "remove", "--force", worktree], cwd=repo_dir, check=False)
        shutil.rmtree(worktree, ignore_errors=True)


def _prune_checkpoints(worktree: str, branch: str, keep: int) -> None:
    """Keep only the last N commits by recreating the branch from scratch."""
    # Get the last `keep` commit messages and tree SHAs
    result = run(
        ["git", "log", f"--max-count={keep}", "--reverse", "--format=%H %s"],
        cwd=worktree,
    )
    commits = [line.split(" ", 1) for line in result.stdout.strip().split("\n") if line]

    # Recreate orphan branch with only the last N snapshots
    run(["git", "checkout", "--orphan", "temp-prune"], cwd=worktree)
    run(["git", "rm", "-rf", "."], cwd=worktree, check=False)

    for i, (sha, msg) in enumerate(commits):
        # Restore mirror.db from each historical commit
        run(["git", "checkout", sha, "--", "mirror.db"], cwd=worktree)
        run(["git", "add", "mirror.db"], cwd=worktree)
        if i == 0:
            run(["git", "commit", "--allow-empty", "-m", msg], cwd=worktree)
        else:
            # Check if there's actually a diff from previous
            diff = run(["git", "diff", "--cached", "--quiet"], cwd=worktree, check=False)
            if diff.returncode != 0:
                run(["git", "commit", "-m", msg], cwd=worktree)
            else:
                run(["git", "commit", "--allow-empty", "-m", msg], cwd=worktree)

    run(["git", "branch", "-D", branch], cwd=worktree, check=False)
    run(["git", "branch", "-m", branch], cwd=worktree)


def main() -> None:
    parser = argparse.ArgumentParser(description="Checkpoint mirror DB to git branch")
    parser.add_argument("--db", required=True, help="Path to mirror.db")
    parser.add_argument("--repo-dir", default=".", help="Git repo directory")
    parser.add_argument("--branch", default="memex-data", help="Branch name for checkpoints")
    parser.add_argument("--max", type=int, default=5, help="Max checkpoints to keep")
    args = parser.parse_args()

    success = checkpoint(args.db, args.repo_dir, args.branch, args.max)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
