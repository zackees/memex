"""Memex: Build a SQLite FTS5 search index from a GitHub repository.

Creates separate tables per source type (files, issues, pull_requests,
commits, wiki) plus unified FTS5 indexes across all sources.

Usage:
  python action/build_index.py --repo owner/repo --repo-dir . --output index.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEXT_EXTENSIONS = {
    ".md", ".txt", ".rst", ".py", ".rs", ".js", ".ts", ".jsx", ".tsx",
    ".yaml", ".yml", ".toml", ".json", ".cfg", ".ini", ".conf",
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".ps1",
    ".html", ".css", ".scss", ".less", ".svg",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx", ".ino",
    ".go", ".java", ".rb", ".php", ".swift", ".kt", ".scala",
    ".sql", ".graphql", ".proto",
    ".dockerfile", ".gitignore", ".env.example",
    ".cmake", ".make", ".mk",
    ".r", ".R", ".jl", ".lua", ".pl", ".pm",
}

SKIP_PARTS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".eggs", "target", "vendor", ".cache",
    "package-lock.json", "yarn.lock", "Cargo.lock", "uv.lock",
    "pnpm-lock.yaml", "composer.lock", "Gemfile.lock",
}

BARE_FILENAMES = {
    "Makefile", "Dockerfile", "Procfile", "Gemfile", "Rakefile",
    "Vagrantfile", "LICENSE", "CODEOWNERS", "OWNERS",
    "CMakeLists.txt", "meson.build",
}

MAX_FILE_SIZE = 512 * 1024  # 512KB


def should_skip(path: Path) -> bool:
    for part in path.parts:
        if part in SKIP_PARTS:
            return True
    return False


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if not path.suffix and path.name in BARE_FILENAMES:
        return True
    return False


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------

SCHEMA = """
-- Per-source tables
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS pull_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS wiki (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT
);

-- Unified chunks view for FTS
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT
);

-- FTS5 trigram index (substring/fuzzy matching)
CREATE VIRTUAL TABLE IF NOT EXISTS search_trigram USING fts5(
    source_type, path, title, body, metadata,
    content=chunks, content_rowid=id,
    tokenize='trigram'
);

-- FTS5 porter index (stemmed whole-word search)
CREATE VIRTUAL TABLE IF NOT EXISTS search_porter USING fts5(
    source_type, path, title, body, metadata,
    content=chunks, content_rowid=id,
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync with chunks
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO search_trigram(rowid, source_type, path, title, body, metadata)
        VALUES (new.id, new.source_type, new.path, new.title, new.body, new.metadata);
    INSERT INTO search_porter(rowid, source_type, path, title, body, metadata)
        VALUES (new.id, new.source_type, new.path, new.title, new.body, new.metadata);
END;

-- Build metadata
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def insert_chunk(conn: sqlite3.Connection, source_type: str, path: str, title: str, body: str, metadata: str) -> None:
    """Insert into both the source-specific table and the unified chunks table."""
    table_map = {
        "file": "files",
        "issue": "issues",
        "pr": "pull_requests",
        "commit": "commits",
        "wiki": "wiki",
    }
    table = table_map.get(source_type)
    if table:
        conn.execute(
            f"INSERT INTO {table} (path, title, body, metadata) VALUES (?, ?, ?, ?)",
            (path, title, body, metadata),
        )
    conn.execute(
        "INSERT INTO chunks (source_type, path, title, body, metadata) VALUES (?, ?, ?, ?, ?)",
        (source_type, path, title, body, metadata),
    )


# ---------------------------------------------------------------------------
# Indexers
# ---------------------------------------------------------------------------

def index_files(conn: sqlite3.Connection, repo_dir: Path) -> int:
    count = 0
    skipped_ext: dict[str, int] = {}
    total_seen = 0
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file():
            continue
        total_seen += 1
        if should_skip(path):
            continue
        if not is_text_file(path):
            ext = path.suffix.lower() or "(none)"
            skipped_ext[ext] = skipped_ext.get(ext, 0) + 1
            continue
        if path.stat().st_size > MAX_FILE_SIZE:
            continue

        rel_path = str(path.relative_to(repo_dir))
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        metadata = json.dumps({
            "size": path.stat().st_size,
            "lines": body.count("\n") + 1,
            "extension": path.suffix,
        })
        insert_chunk(conn, "file", rel_path, path.name, body, metadata)
        count += 1

    if count == 0:
        print(f"    DEBUG: repo_dir={repo_dir}, exists={repo_dir.exists()}, is_dir={repo_dir.is_dir()}")
        print(f"    DEBUG: total files seen={total_seen}")
        top_skipped = sorted(skipped_ext.items(), key=lambda x: -x[1])[:10]
        if top_skipped:
            print(f"    DEBUG: top skipped extensions: {top_skipped}")
        # List first 10 files in repo root
        try:
            entries = list(repo_dir.iterdir())[:10]
            print(f"    DEBUG: first entries in repo_dir: {[e.name for e in entries]}")
        except Exception as e:
            print(f"    DEBUG: error listing repo_dir: {e}")

    return count


def index_commits(conn: sqlite3.Connection, repo_dir: Path, limit: int = 500) -> int:
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={limit}",
             "--format=%H%x00%s%x00%b%x00%an%x00%ai", "--no-merges"],
            cwd=repo_dir, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return 0
    except Exception:
        return 0

    count = 0
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\0")
        if len(parts) < 5:
            continue
        sha, subject, body_text, author, date = parts[0], parts[1], parts[2], parts[3], parts[4]
        metadata = json.dumps({"sha": sha, "author": author, "date": date})
        insert_chunk(conn, "commit", sha[:8], subject, f"{subject}\n\n{body_text}".strip(), metadata)
        count += 1
    return count


def gh_api(endpoint: str, paginate: bool = True) -> Any:
    """Call GitHub API via gh CLI."""
    try:
        cmd = ["gh", "api", endpoint]
        if paginate:
            cmd.append("--paginate")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def index_issues(conn: sqlite3.Connection, repo: str) -> int:
    issues = gh_api(f"repos/{repo}/issues?state=all&per_page=100")
    if not issues or not isinstance(issues, list):
        return 0

    count = 0
    for issue in issues:
        if "pull_request" in issue:
            continue

        labels = [label.get("name", "") for label in issue.get("labels", []) if isinstance(label, dict)]
        metadata = json.dumps({
            "number": issue["number"],
            "state": issue["state"],
            "author": issue.get("user", {}).get("login", ""),
            "labels": labels,
            "created_at": issue.get("created_at", ""),
            "updated_at": issue.get("updated_at", ""),
            "comments_count": issue.get("comments", 0),
        })

        body_text = issue.get("body") or ""
        comments = gh_api(f"repos/{repo}/issues/{issue['number']}/comments?per_page=100")
        if comments and isinstance(comments, list):
            for c in comments:
                c_body = c.get("body", "")
                if c_body:
                    c_author = c.get("user", {}).get("login", "unknown")
                    body_text += f"\n\n---\n**{c_author}**: {c_body}"

        insert_chunk(conn, "issue", f"#{issue['number']}", issue.get("title", ""), body_text, metadata)
        count += 1
    return count


def index_pull_requests(conn: sqlite3.Connection, repo: str) -> int:
    prs = gh_api(f"repos/{repo}/pulls?state=all&per_page=100")
    if not prs or not isinstance(prs, list):
        return 0

    count = 0
    for pr in prs:
        metadata = json.dumps({
            "number": pr["number"],
            "state": pr["state"],
            "author": pr.get("user", {}).get("login", ""),
            "merged": pr.get("merged_at") is not None,
            "created_at": pr.get("created_at", ""),
            "base": pr.get("base", {}).get("ref", ""),
            "head": pr.get("head", {}).get("ref", ""),
        })

        body_text = pr.get("body") or ""
        comments = gh_api(f"repos/{repo}/pulls/{pr['number']}/comments?per_page=100")
        if comments and isinstance(comments, list):
            for c in comments:
                c_body = c.get("body", "")
                if c_body:
                    c_author = c.get("user", {}).get("login", "unknown")
                    c_path = c.get("path", "")
                    body_text += f"\n\n---\n**{c_author}** on `{c_path}`: {c_body}"

        insert_chunk(conn, "pr", f"PR#{pr['number']}", pr.get("title", ""), body_text, metadata)
        count += 1
    return count


def index_wiki(conn: sqlite3.Connection, repo: str, repo_dir: Path) -> int:
    wiki_dir = repo_dir.parent / (repo_dir.name + ".wiki")
    if not wiki_dir.is_dir():
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", f"https://github.com/{repo}.wiki.git", str(wiki_dir)],
                capture_output=True, text=True, timeout=60,
            )
        except Exception:
            pass

    if not wiki_dir.is_dir():
        return 0

    count = 0
    for path in sorted(wiki_dir.rglob("*.md")):
        if should_skip(path):
            continue
        rel_path = str(path.relative_to(wiki_dir))
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        title = path.stem.replace("-", " ")
        metadata = json.dumps({"wiki_page": rel_path})
        insert_chunk(conn, "wiki", rel_path, title, body, metadata)
        count += 1
    return count


def optimize_for_http(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA page_size = 1024")
    conn.execute("VACUUM")
    conn.execute("PRAGMA journal_mode = DELETE")


def main() -> None:
    parser = argparse.ArgumentParser(description="Memex: Build FTS5 search index from a GitHub repo")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                        help="GitHub repo (owner/repo)")
    parser.add_argument("--repo-dir", default=".", help="Path to the cloned repo")
    parser.add_argument("--subdir", default="", help="Subdirectory to index for files (e.g. 'src'). Empty = whole repo.")
    parser.add_argument("--output", default="index.db", help="Output SQLite database path")
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    output = Path(args.output).resolve()

    if output.exists():
        output.unlink()

    print(f"Memex: building index for {args.repo}")
    print(f"  Repo dir: {repo_dir}")
    print(f"  Output: {output}")

    conn = sqlite3.connect(str(output))
    create_tables(conn)

    files_dir = repo_dir / args.subdir if args.subdir else repo_dir
    if args.subdir:
        print(f"  Indexing files from subdir: {args.subdir}")
    n_files = index_files(conn, files_dir)
    print(f"  files: {n_files}")

    n_commits = index_commits(conn, repo_dir)
    print(f"  commits: {n_commits}")

    n_issues = 0
    n_prs = 0
    n_wiki = 0
    if args.repo:
        n_issues = index_issues(conn, args.repo)
        print(f"  issues: {n_issues}")

        n_prs = index_pull_requests(conn, args.repo)
        print(f"  pull_requests: {n_prs}")

        n_wiki = index_wiki(conn, args.repo, repo_dir)
        print(f"  wiki: {n_wiki}")
    else:
        print("  (no --repo, skipping GitHub API sources)")

    total = n_files + n_commits + n_issues + n_prs + n_wiki
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('repo', ?)", (args.repo,))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('total_chunks', ?)", (str(total),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('files', ?)", (str(n_files),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('commits', ?)", (str(n_commits),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('issues', ?)", (str(n_issues),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('pull_requests', ?)", (str(n_prs),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('wiki', ?)", (str(n_wiki),))
    conn.commit()

    print("  Optimizing for HTTP range requests...")
    optimize_for_http(conn)
    conn.close()

    size_kb = output.stat().st_size / 1024
    print(f"Done! {total} chunks, {size_kb:.1f} KB")


if __name__ == "__main__":
    main()
