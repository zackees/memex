"""Build presentation database from mirror DB.

Transforms raw GitHub data into an FTS5-indexed search database
optimized for HTTP range request queries.

Usage:
  python action/build_presentation.py --mirror mirror.db --output index.db
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Presentation schema
# ---------------------------------------------------------------------------

PRESENTATION_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Issues & PRs (unified)
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY,
    number          INTEGER NOT NULL,
    entity_type     TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    labels          TEXT NOT NULL DEFAULT '',
    author          TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    closed_at       TEXT,
    merged_at       TEXT,
    comment_count   INTEGER NOT NULL DEFAULT 0,
    reaction_count  INTEGER NOT NULL DEFAULT 0,
    ref_count       INTEGER NOT NULL DEFAULT 0,
    additions       INTEGER,
    deletions       INTEGER,
    changed_files   INTEGER,
    head_ref        TEXT,
    base_ref        TEXT,
    UNIQUE(entity_type, number)
);

CREATE INDEX IF NOT EXISTS idx_items_type    ON items(entity_type);
CREATE INDEX IF NOT EXISTS idx_items_state   ON items(state);
CREATE INDEX IF NOT EXISTS idx_items_author  ON items(author);
CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at DESC);

-- Porter FTS for natural language search on items
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, body, labels, author,
    content='items', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

-- Trigram FTS for substring search on items
CREATE VIRTUAL TABLE IF NOT EXISTS items_trigram USING fts5(
    title, body, labels, author,
    content='items', content_rowid='id',
    tokenize='trigram'
);

-- Triggers: items
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body, labels, author)
        VALUES (new.id, new.title, new.body, new.labels, new.author);
    INSERT INTO items_trigram(rowid, title, body, labels, author)
        VALUES (new.id, new.title, new.body, new.labels, new.author);
END;

-- Commits
CREATE TABLE IF NOT EXISTS commits (
    id              INTEGER PRIMARY KEY,
    sha             TEXT NOT NULL UNIQUE,
    subject         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    author          TEXT NOT NULL DEFAULT '',
    committed_at    TEXT NOT NULL DEFAULT '',
    additions       INTEGER NOT NULL DEFAULT 0,
    deletions       INTEGER NOT NULL DEFAULT 0,
    files_changed   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_commits_author ON commits(author);
CREATE INDEX IF NOT EXISTS idx_commits_date   ON commits(committed_at DESC);

-- Porter FTS for commits
CREATE VIRTUAL TABLE IF NOT EXISTS commits_fts USING fts5(
    subject, body, author, files_changed,
    content='commits', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

-- Trigram FTS for commits
CREATE VIRTUAL TABLE IF NOT EXISTS commits_trigram USING fts5(
    subject, body, author, files_changed,
    content='commits', content_rowid='id',
    tokenize='trigram'
);

-- Triggers: commits
CREATE TRIGGER IF NOT EXISTS commits_ai AFTER INSERT ON commits BEGIN
    INSERT INTO commits_fts(rowid, subject, body, author, files_changed)
        VALUES (new.id, new.subject, new.body, new.author, new.files_changed);
    INSERT INTO commits_trigram(rowid, subject, body, author, files_changed)
        VALUES (new.id, new.subject, new.body, new.author, new.files_changed);
END;

-- Comments (searchable, linked to parent)
CREATE TABLE IF NOT EXISTS comments (
    id              INTEGER PRIMARY KEY,
    parent_type     TEXT NOT NULL,
    parent_number   INTEGER NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_type, parent_number);

CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
    author, body,
    content='comments', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS comments_ai AFTER INSERT ON comments BEGIN
    INSERT INTO comments_fts(rowid, author, body)
        VALUES (new.id, new.author, new.body);
END;

-- Cross-references
CREATE TABLE IF NOT EXISTS cross_refs (
    id              INTEGER PRIMARY KEY,
    source_type     TEXT NOT NULL,
    source_number   INTEGER NOT NULL,
    target_type     TEXT NOT NULL,
    target_number   INTEGER NOT NULL,
    ref_type        TEXT NOT NULL,
    UNIQUE(source_type, source_number, target_type, target_number, ref_type)
);

CREATE INDEX IF NOT EXISTS idx_xref_target ON cross_refs(target_type, target_number);

-- Author identity resolution (email -> GitHub username)
CREATE TABLE IF NOT EXISTS author_map (
    email TEXT PRIMARY KEY,
    github_user TEXT NOT NULL
);

-- Build metadata
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


# ---------------------------------------------------------------------------
# Identity resolution: email -> GitHub username
# ---------------------------------------------------------------------------

NOREPLY_RE = re.compile(r'(?:\d+\+)?(.+)@users\.noreply\.github\.com')


def build_author_map(mirror: sqlite3.Connection) -> dict[str, str]:
    """Build email -> GitHub username mapping from mirror data.

    Sources (in priority order):
    1. GitHub noreply emails: 12345+username@users.noreply.github.com
    2. PR merge commit cross-reference: commit email -> PR author
    3. Issue/PR author names used as fallback identity
    """
    email_to_gh: dict[str, str] = {}

    # 1. Noreply email pattern
    for row in mirror.execute(
        "SELECT DISTINCT author_email FROM commits WHERE author_email LIKE '%@users.noreply.github.com'"
    ):
        m = NOREPLY_RE.match(row[0])
        if m:
            email_to_gh[row[0]] = m.group(1)

    # 2. PR merge commit -> PR author
    for row in mirror.execute("""
        SELECT c.author_email, p.author
        FROM commits c
        JOIN pull_requests p ON c.sha = p.merge_commit_sha
        WHERE p.author IS NOT NULL AND p.author != ''
        GROUP BY c.author_email
    """):
        if row[0] not in email_to_gh:
            email_to_gh[row[0]] = row[1]

    # 3. Match commit author names to known GitHub usernames from issues/PRs
    # Build set of known GitHub usernames (lowercase -> canonical)
    gh_usernames: dict[str, str] = {}
    for row in mirror.execute("SELECT DISTINCT author FROM issues WHERE author IS NOT NULL AND author != ''"):
        gh_usernames[row[0].lower()] = row[0]
    for row in mirror.execute("SELECT DISTINCT author FROM pull_requests WHERE author IS NOT NULL AND author != ''"):
        gh_usernames[row[0].lower()] = row[0]

    # For unmapped emails, check if the git author name matches a GitHub username
    for row in mirror.execute("""
        SELECT DISTINCT author_email, author_name FROM commits
        WHERE author_email NOT IN ({})
    """.format(",".join("?" * len(email_to_gh))), list(email_to_gh.keys()) if email_to_gh else [""]):
        name_lower = (row[1] or "").lower()
        if name_lower in gh_usernames:
            email_to_gh[row[0]] = gh_usernames[name_lower]

    # 4. Group emails that resolve to the same GitHub user
    # Build reverse map: github_user -> [emails]
    gh_to_emails: dict[str, list[str]] = {}
    for email, gh in email_to_gh.items():
        gh_to_emails.setdefault(gh, []).append(email)

    return email_to_gh


def resolve_commit_author(author_name: str, author_email: str, author_map: dict[str, str]) -> str:
    """Resolve a commit author to their GitHub username if possible."""
    if author_email in author_map:
        return author_map[author_email]
    return author_name or ""


# ---------------------------------------------------------------------------
# Cross-reference extraction
# ---------------------------------------------------------------------------

# Matches #123, GH-123, owner/repo#123
ISSUE_REF_RE = re.compile(r'(?:^|\s)(?:(?:[\w.-]+/[\w.-]+)?#|GH-)(\d+)', re.MULTILINE)

# Matches "closes #123", "fixes #123", "resolves #123"
CLOSES_RE = re.compile(
    r'(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)',
    re.IGNORECASE,
)


def extract_refs(text: str) -> tuple[set[int], set[int]]:
    """Extract issue/PR references from text. Returns (mentions, closes)."""
    if not text:
        return set(), set()
    mentions = {int(m) for m in ISSUE_REF_RE.findall(text)}
    closes = {int(m) for m in CLOSES_RE.findall(text)}
    return mentions, closes


# ---------------------------------------------------------------------------
# Transform mirror → presentation
# ---------------------------------------------------------------------------

def build_items(mirror: sqlite3.Connection, pres: sqlite3.Connection) -> int:
    """Copy issues and PRs from mirror to presentation DB."""
    count = 0

    # Issues
    for row in mirror.execute("""
        SELECT number, title, state, state_reason, author, body,
               labels, created_at, updated_at, closed_at,
               comments_count, reactions_count
        FROM issues
    """):
        number, title, state, state_reason, author, body, labels_json, \
            created_at, updated_at, closed_at, comment_count, reaction_count = row

        labels = " ".join(json.loads(labels_json)) if labels_json else ""
        st = state.lower() if state else "open"

        pres.execute("""
            INSERT INTO items (number, entity_type, title, body, labels, author,
                state, created_at, updated_at, closed_at, comment_count, reaction_count)
            VALUES (?, 'issue', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (number, title, body or "", labels, author or "",
              st, created_at or "", updated_at or "", closed_at,
              comment_count or 0, reaction_count or 0))
        count += 1

    # PRs
    for row in mirror.execute("""
        SELECT number, title, state, author, body, labels,
               created_at, updated_at, closed_at, merged_at,
               additions, deletions, changed_files,
               head_ref, base_ref, comments_count, review_comments_count
        FROM pull_requests
    """):
        number, title, state, author, body, labels_json, \
            created_at, updated_at, closed_at, merged_at, \
            additions, deletions, changed_files, \
            head_ref, base_ref, comment_count, review_comment_count = row

        labels = " ".join(json.loads(labels_json)) if labels_json else ""
        st = "merged" if merged_at else (state.lower() if state else "open")

        pres.execute("""
            INSERT INTO items (number, entity_type, title, body, labels, author,
                state, created_at, updated_at, closed_at, merged_at,
                comment_count, additions, deletions, changed_files,
                head_ref, base_ref)
            VALUES (?, 'pr', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (number, title, body or "", labels, author or "",
              st, created_at or "", updated_at or "", closed_at, merged_at,
              (comment_count or 0) + (review_comment_count or 0),
              additions, deletions, changed_files,
              head_ref or "", base_ref or ""))
        count += 1

    pres.commit()
    return count


def build_commits(mirror: sqlite3.Connection, pres: sqlite3.Connection,
                  author_map: dict[str, str]) -> int:
    """Copy commits from mirror to presentation DB with resolved authors."""
    count = 0
    for row in mirror.execute("""
        SELECT sha, author_name, author_email, author_date, message,
               files_changed, additions, deletions, file_stats
        FROM commits
    """):
        sha, author_name, author_email, date, message, n_files, additions, deletions, file_stats_json = row

        author = resolve_commit_author(author_name or "", author_email or "", author_map)

        # Split message into subject + body
        parts = (message or "").split("\n", 1)
        subject = parts[0].strip()
        body = parts[1].strip() if len(parts) > 1 else ""

        # Build space-separated file paths for search
        files_text = ""
        if file_stats_json:
            try:
                files = json.loads(file_stats_json)
                files_text = " ".join(f.get("path", "") for f in files)
            except (json.JSONDecodeError, TypeError):
                pass

        pres.execute("""
            INSERT INTO commits (sha, subject, body, author, committed_at,
                additions, deletions, files_changed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (sha, subject, body, author or "", date or "",
              additions or 0, deletions or 0, files_text))
        count += 1

    pres.commit()
    return count


def build_comments(mirror: sqlite3.Connection, pres: sqlite3.Connection) -> int:
    """Copy comments from mirror to presentation DB."""
    count = 0

    # Issue comments
    for row in mirror.execute("""
        SELECT id, issue_number, author, body, created_at
        FROM issue_comments
    """):
        cid, issue_number, author, body, created_at = row

        # Determine if this belongs to an issue or PR
        is_pr = mirror.execute(
            "SELECT 1 FROM pull_requests WHERE number = ?", (issue_number,)
        ).fetchone()
        parent_type = "pr" if is_pr else "issue"

        pres.execute("""
            INSERT INTO comments (id, parent_type, parent_number, author, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (cid, parent_type, issue_number, author or "", body or "", created_at or ""))
        count += 1

    # PR review comments
    for row in mirror.execute("""
        SELECT id, pr_number, author, body, created_at
        FROM pr_review_comments
    """):
        cid, pr_number, author, body, created_at = row
        pres.execute("""
            INSERT OR IGNORE INTO comments (id, parent_type, parent_number, author, body, created_at)
            VALUES (?, 'pr', ?, ?, ?, ?)
        """, (cid, pr_number, author or "", body or "", created_at or ""))
        count += 1

    pres.commit()
    return count


def build_cross_refs(pres: sqlite3.Connection) -> int:
    """Extract cross-references from item bodies and commit messages."""
    count = 0

    # From items (issues + PRs)
    for row in pres.execute("SELECT id, entity_type, number, body FROM items"):
        item_id, etype, number, body = row
        mentions, closes = extract_refs(body or "")
        for target in mentions:
            if target == number:
                continue  # skip self-references
            try:
                pres.execute("""
                    INSERT OR IGNORE INTO cross_refs
                        (source_type, source_number, target_type, target_number, ref_type)
                    VALUES (?, ?, 'issue', ?, 'mentions')
                """, (etype, number, target))
                count += 1
            except sqlite3.IntegrityError:
                pass
        for target in closes:
            if target == number:
                continue
            try:
                pres.execute("""
                    INSERT OR IGNORE INTO cross_refs
                        (source_type, source_number, target_type, target_number, ref_type)
                    VALUES (?, ?, 'issue', ?, 'closes')
                """, (etype, number, target))
                count += 1
            except sqlite3.IntegrityError:
                pass

    # From commits
    for row in pres.execute("SELECT id, sha, subject, body FROM commits"):
        commit_id, sha, subject, body = row
        text = f"{subject}\n{body}"
        mentions, closes = extract_refs(text)
        for target in mentions:
            try:
                pres.execute("""
                    INSERT OR IGNORE INTO cross_refs
                        (source_type, source_number, target_type, target_number, ref_type)
                    VALUES ('commit', ?, 'issue', ?, 'mentions')
                """, (commit_id, target))
                count += 1
            except sqlite3.IntegrityError:
                pass
        for target in closes:
            try:
                pres.execute("""
                    INSERT OR IGNORE INTO cross_refs
                        (source_type, source_number, target_type, target_number, ref_type)
                    VALUES ('commit', ?, 'issue', ?, 'closes')
                """, (commit_id, target))
                count += 1
            except sqlite3.IntegrityError:
                pass

    # Update ref_counts on items
    pres.execute("""
        UPDATE items SET ref_count = (
            SELECT COUNT(*) FROM cross_refs
            WHERE target_type = items.entity_type AND target_number = items.number
        )
    """)

    pres.commit()
    return count


def optimize_for_http(conn: sqlite3.Connection) -> None:
    """Optimize database for HTTP range request serving."""
    conn.execute("INSERT INTO items_fts(items_fts) VALUES ('optimize')")
    conn.execute("INSERT INTO items_trigram(items_trigram) VALUES ('optimize')")
    conn.execute("INSERT INTO commits_fts(commits_fts) VALUES ('optimize')")
    conn.execute("INSERT INTO commits_trigram(commits_trigram) VALUES ('optimize')")
    conn.execute("INSERT INTO comments_fts(comments_fts) VALUES ('optimize')")
    conn.commit()
    # Must switch OFF WAL before VACUUM can change page_size
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA page_size = 1024")
    conn.execute("VACUUM")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build presentation DB from mirror")
    parser.add_argument("--mirror", required=True, help="Path to mirror.db")
    parser.add_argument("--output", default="index.db", help="Output presentation DB")
    parser.add_argument("--repo", default="", help="Repo name for metadata")
    args = parser.parse_args()

    if not os.path.isfile(args.mirror):
        print(f"ERROR: Mirror DB not found: {args.mirror}")
        return

    output = Path(args.output).resolve()
    if output.exists():
        output.unlink()

    print(f"Building presentation DB from {args.mirror}")

    mirror = sqlite3.connect(args.mirror)
    pres = sqlite3.connect(str(output))
    pres.executescript(PRESENTATION_SCHEMA)

    # Build author identity map
    author_map = build_author_map(mirror)
    # Persist to presentation DB (never re-resolve)
    for email, gh_user in author_map.items():
        pres.execute("INSERT OR IGNORE INTO author_map (email, github_user) VALUES (?, ?)",
                     (email, gh_user))
    pres.commit()
    print(f"  Author identities resolved: {len(author_map)} emails mapped")

    # Build all tables
    n_items = build_items(mirror, pres)
    print(f"  Items (issues + PRs): {n_items}")

    n_commits = build_commits(mirror, pres, author_map)
    print(f"  Commits: {n_commits}")

    n_comments = build_comments(mirror, pres)
    print(f"  Comments: {n_comments}")

    n_refs = build_cross_refs(pres)
    print(f"  Cross-references: {n_refs}")

    # Metadata
    repo = args.repo
    if not repo:
        row = mirror.execute("SELECT value FROM repo_meta WHERE key = 'full_name'").fetchone()
        if row:
            repo = row[0]
    pres.execute("INSERT OR REPLACE INTO meta VALUES ('repo', ?)", (repo,))
    pres.execute("INSERT OR REPLACE INTO meta VALUES ('total_items', ?)", (str(n_items),))
    pres.execute("INSERT OR REPLACE INTO meta VALUES ('total_commits', ?)", (str(n_commits),))
    pres.execute("INSERT OR REPLACE INTO meta VALUES ('total_comments', ?)", (str(n_comments),))
    pres.execute("INSERT OR REPLACE INTO meta VALUES ('total_cross_refs', ?)", (str(n_refs),))
    pres.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
                 (datetime.now(timezone.utc).isoformat(),))
    pres.commit()

    # Optimize
    print("  Optimizing for HTTP range requests...")
    optimize_for_http(pres)

    mirror.close()
    pres.close()

    size_mb = output.stat().st_size / 1024 / 1024
    print(f"Done! {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
