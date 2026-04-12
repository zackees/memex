"""Sync git commits into the mirror database from local git log.

Zero API calls — reads directly from the local git repository.
Always full depth (message + file stats).

Usage:
  python action/index_commits.py --repo-dir /path/to/repo --db mirror.db
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mirror_schema import create_mirror_db


def index_commits(
    conn: Any,
    repo_dir: Path,
    limit: int = 5000,
) -> int:
    """Index commits from git log --numstat. Returns count synced."""
    now = datetime.now(timezone.utc).isoformat()

    # Check what we already have
    row = conn.execute("SELECT MAX(author_date) FROM commits").fetchone()
    since = row[0] if row and row[0] else None

    # Build git log command
    # Format: SHA\0author_name\0author_email\0author_date\0subject\0body\0
    # Then --numstat gives per-file stats
    cmd = [
        "git", "log",
        f"--max-count={limit}",
        "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x00%b%x00",
        "--numstat",
    ]
    if since:
        cmd.append(f"--since={since}")
        print(f"  Incremental: commits after {since}")

    try:
        result = subprocess.run(
            cmd, cwd=repo_dir, capture_output=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print(f"  git log failed: {(result.stderr or '')[:200]}")
            return 0
    except Exception as e:
        print(f"  git log error: {e}")
        return 0

    # Parse the output
    # Each commit block: header line (fields separated by \0) followed by numstat lines
    # Numstat lines: "additions\tdeletions\tfilename" or empty line between commits
    count = 0
    current_sha = None
    current_fields: dict[str, Any] = {}
    current_files: list[dict[str, Any]] = []
    total_add = 0
    total_del = 0

    def flush() -> None:
        nonlocal count, current_sha, current_files, total_add, total_del
        if not current_sha:
            return
        conn.execute("""
            INSERT INTO commits (sha, author_name, author_email, author_date,
                message, files_changed, additions, deletions, file_stats, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha) DO UPDATE SET
                message=excluded.message, file_stats=excluded.file_stats,
                files_changed=excluded.files_changed,
                additions=excluded.additions, deletions=excluded.deletions,
                synced_at=excluded.synced_at
        """, (
            current_sha,
            current_fields.get("author_name", ""),
            current_fields.get("author_email", ""),
            current_fields.get("author_date", ""),
            current_fields.get("message", ""),
            len(current_files),
            total_add,
            total_del,
            json.dumps(current_files) if current_files else None,
            now,
        ))
        count += 1
        current_sha = None
        current_files = []
        total_add = 0
        total_del = 0

    for line in result.stdout.split("\n"):
        if "\0" in line:
            # This is a commit header line
            flush()
            parts = line.split("\0")
            if len(parts) >= 6:
                current_sha = parts[0]
                current_fields = {
                    "author_name": parts[1],
                    "author_email": parts[2],
                    "author_date": parts[3],
                    "message": (parts[4] + "\n\n" + parts[5]).strip(),
                }
        elif line.strip() and current_sha and "\t" in line:
            # This is a numstat line: "additions\tdeletions\tfilename"
            numstat_parts = line.split("\t", 2)
            if len(numstat_parts) == 3:
                add_str, del_str, filepath = numstat_parts
                try:
                    add = int(add_str) if add_str != "-" else 0
                    dele = int(del_str) if del_str != "-" else 0
                except ValueError:
                    add, dele = 0, 0
                current_files.append({
                    "path": filepath,
                    "additions": add,
                    "deletions": dele,
                })
                total_add += add
                total_del += dele

    flush()
    conn.commit()
    return count


def log_sync(conn: Any, items: int, started: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO sync_log (entity_type, depth, items_synced, api_calls_used, started_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("commits", 99, items, 0, started, now))  # depth=99 means always full
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync git commits to mirror DB")
    parser.add_argument("--repo-dir", required=True, help="Path to git repo")
    parser.add_argument("--db", default="mirror.db", help="Mirror database path")
    parser.add_argument("--limit", type=int, default=5000, help="Max commits to index")
    parser.add_argument("--repo", default="", help="Repo name for metadata (owner/repo)")
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    if not (repo_dir / ".git").is_dir():
        print(f"ERROR: {repo_dir} is not a git repository")
        return

    conn = create_mirror_db(args.db)
    started = datetime.now(timezone.utc).isoformat()

    print(f"Indexing commits from {repo_dir}...")
    n = index_commits(conn, repo_dir, limit=args.limit)
    print(f"Commits synced: {n} (0 API calls)")
    log_sync(conn, n, started)

    # Summary
    total = conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    total_add = conn.execute("SELECT SUM(additions) FROM commits").fetchone()[0] or 0
    total_del = conn.execute("SELECT SUM(deletions) FROM commits").fetchone()[0] or 0
    print(f"Mirror DB: {total} commits, +{total_add}/-{total_del} lines")

    conn.close()


if __name__ == "__main__":
    main()
