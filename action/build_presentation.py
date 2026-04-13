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
PRAGMA journal_mode = DELETE;
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

-- Contributor analytics
CREATE TABLE IF NOT EXISTS contributor_stats (
    author              TEXT PRIMARY KEY,
    commit_count        INTEGER NOT NULL DEFAULT 0,
    additions           INTEGER NOT NULL DEFAULT 0,
    deletions           INTEGER NOT NULL DEFAULT 0,
    issues_opened       INTEGER NOT NULL DEFAULT 0,
    prs_opened          INTEGER NOT NULL DEFAULT 0,
    issue_comments      INTEGER NOT NULL DEFAULT 0,
    pr_comments         INTEGER NOT NULL DEFAULT 0,
    reviews_submitted   INTEGER NOT NULL DEFAULT 0,
    review_comments     INTEGER NOT NULL DEFAULT 0,
    total_items         INTEGER NOT NULL DEFAULT 0,
    total_comments      INTEGER NOT NULL DEFAULT 0,
    activity_score      INTEGER NOT NULL DEFAULT 0,
    first_active_at     TEXT NOT NULL DEFAULT '',
    last_active_at      TEXT NOT NULL DEFAULT '',
    top_year            INTEGER,
    top_year_commits    INTEGER NOT NULL DEFAULT 0,
    top_year_activity   INTEGER NOT NULL DEFAULT 0,
    primary_role        TEXT NOT NULL DEFAULT '',
    summary             TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_contributor_score
    ON contributor_stats(activity_score DESC, commit_count DESC, author);

CREATE TABLE IF NOT EXISTS contributor_yearly (
    author              TEXT NOT NULL,
    year                INTEGER NOT NULL,
    commit_count        INTEGER NOT NULL DEFAULT 0,
    additions           INTEGER NOT NULL DEFAULT 0,
    deletions           INTEGER NOT NULL DEFAULT 0,
    issues_opened       INTEGER NOT NULL DEFAULT 0,
    prs_opened          INTEGER NOT NULL DEFAULT 0,
    issue_comments      INTEGER NOT NULL DEFAULT 0,
    pr_comments         INTEGER NOT NULL DEFAULT 0,
    reviews_submitted   INTEGER NOT NULL DEFAULT 0,
    review_comments     INTEGER NOT NULL DEFAULT 0,
    total_items         INTEGER NOT NULL DEFAULT 0,
    total_comments      INTEGER NOT NULL DEFAULT 0,
    activity_score      INTEGER NOT NULL DEFAULT 0,
    first_active_at     TEXT NOT NULL DEFAULT '',
    last_active_at      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (author, year)
);

CREATE INDEX IF NOT EXISTS idx_contributor_yearly_rank
    ON contributor_yearly(year DESC, activity_score DESC, commit_count DESC, author);

CREATE TABLE IF NOT EXISTS reference_points (
    category        TEXT NOT NULL,
    metric          TEXT NOT NULL,
    scope           TEXT NOT NULL,
    rank            INTEGER NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_key      TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    value_int       INTEGER NOT NULL DEFAULT 0,
    secondary_value INTEGER NOT NULL DEFAULT 0,
    details         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (category, metric, scope, rank)
);

CREATE INDEX IF NOT EXISTS idx_reference_points_metric
    ON reference_points(category, metric, scope, rank);

CREATE INDEX IF NOT EXISTS idx_reference_points_entity
    ON reference_points(entity_type, entity_key);

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


def empty_contributor_row() -> dict[str, int | str]:
    """Return a zeroed contributor aggregate row."""
    return {
        "commit_count": 0,
        "additions": 0,
        "deletions": 0,
        "issues_opened": 0,
        "prs_opened": 0,
        "issue_comments": 0,
        "pr_comments": 0,
        "reviews_submitted": 0,
        "review_comments": 0,
        "first_active_at": "",
        "last_active_at": "",
    }


def extract_year(ts: str) -> int | None:
    """Extract YYYY from an ISO timestamp."""
    if not ts or len(ts) < 4:
        return None
    prefix = ts[:4]
    if prefix.isdigit():
        return int(prefix)
    return None


def update_activity_window(row: dict[str, int | str], ts: str) -> None:
    """Track first/last seen timestamps for a contributor row."""
    if not ts:
        return
    first = str(row.get("first_active_at", ""))
    last = str(row.get("last_active_at", ""))
    if not first or ts < first:
        row["first_active_at"] = ts
    if not last or ts > last:
        row["last_active_at"] = ts


def bump_author(stats: dict[str, dict[str, int | str]], author: str, activity_at: str = "", **updates: int) -> None:
    """Accumulate contributor-level analytics."""
    author = (author or "").strip()
    if not author:
        return

    row = stats.setdefault(author, empty_contributor_row())
    update_activity_window(row, activity_at)

    for key, value in updates.items():
        row[key] = int(row.get(key, 0)) + int(value or 0)


def finalize_contributor_row(row: dict[str, int | str]) -> dict[str, int | str]:
    """Compute rollup fields for a contributor aggregate row."""
    total_items = int(row["issues_opened"]) + int(row["prs_opened"])
    total_comments = (
        int(row["issue_comments"]) +
        int(row["pr_comments"]) +
        int(row["review_comments"])
    )
    activity_score = (
        int(row["commit_count"]) +
        total_items +
        total_comments +
        int(row["reviews_submitted"])
    )
    return {
        **row,
        "total_items": total_items,
        "total_comments": total_comments,
        "activity_score": activity_score,
    }


def infer_primary_role(row: dict[str, int | str]) -> str:
    """Choose a simple contributor role label for query-facing summaries."""
    weights = {
        "coder": int(row["commit_count"]),
        "discussant": int(row["total_comments"]),
        "reporter": int(row["issues_opened"]),
        "author": int(row["prs_opened"]),
        "reviewer": int(row["reviews_submitted"]) + int(row["review_comments"]),
    }
    role, score = max(weights.items(), key=lambda item: item[1])
    return role if score > 0 else "observer"


def summarize_contributor(author: str, row: dict[str, int | str]) -> str:
    """Generate a compact text summary for LLM-friendly querying."""
    parts = []
    if int(row["commit_count"]) > 0:
        parts.append(f"{row['commit_count']} commits")
    if int(row["total_items"]) > 0:
        parts.append(f"{row['total_items']} issues/prs opened")
    if int(row["total_comments"]) > 0:
        parts.append(f"{row['total_comments']} comments")
    if int(row["reviews_submitted"]) > 0:
        parts.append(f"{row['reviews_submitted']} reviews")
    if not parts:
        parts.append("no recorded activity")
    joined = ", ".join(parts)
    return f"{author}: {joined}; primary role {row['primary_role']}."


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


def build_contributor_stats(
    mirror: sqlite3.Connection,
    pres: sqlite3.Connection,
    author_map: dict[str, str],
) -> int:
    """Build contributor-level analytics for website-ready history queries.

    Returns the number of distinct contributors written to contributor_stats.
    """
    stats: dict[str, dict[str, int | str]] = {}
    yearly: dict[tuple[str, int], dict[str, int | str]] = {}
    pr_numbers = {
        row[0] for row in mirror.execute("SELECT number FROM pull_requests")
    }

    for row in mirror.execute("""
        SELECT author_name, author_email, author_date, additions, deletions
        FROM commits
    """):
        author_name, author_email, author_date, additions, deletions = row
        author = resolve_commit_author(author_name or "", author_email or "", author_map)
        ts = author_date or ""
        bump_author(stats, author, activity_at=ts, commit_count=1, additions=additions or 0, deletions=deletions or 0)
        year = extract_year(ts)
        if year is not None:
            bump_author(yearly, f"{author}\0{year}", activity_at=ts, commit_count=1, additions=additions or 0, deletions=deletions or 0)

    for row in mirror.execute("""
        SELECT author, created_at FROM issues
        WHERE author IS NOT NULL AND author != ''
    """):
        author, created_at = row
        ts = created_at or ""
        bump_author(stats, author, activity_at=ts, issues_opened=1)
        year = extract_year(ts)
        if year is not None:
            bump_author(yearly, f"{author}\0{year}", activity_at=ts, issues_opened=1)

    for row in mirror.execute("""
        SELECT author, created_at FROM pull_requests
        WHERE author IS NOT NULL AND author != ''
    """):
        author, created_at = row
        ts = created_at or ""
        bump_author(stats, author, activity_at=ts, prs_opened=1)
        year = extract_year(ts)
        if year is not None:
            bump_author(yearly, f"{author}\0{year}", activity_at=ts, prs_opened=1)

    for row in mirror.execute("""
        SELECT issue_number, author, created_at FROM issue_comments
        WHERE author IS NOT NULL AND author != ''
    """):
        issue_number, author, created_at = row
        ts = created_at or ""
        field = "pr_comments" if issue_number in pr_numbers else "issue_comments"
        bump_author(stats, author, activity_at=ts, **{field: 1})
        year = extract_year(ts)
        if year is not None:
            bump_author(yearly, f"{author}\0{year}", activity_at=ts, **{field: 1})

    for row in mirror.execute("""
        SELECT author, submitted_at FROM pr_reviews
        WHERE author IS NOT NULL AND author != ''
    """):
        author, submitted_at = row
        ts = submitted_at or ""
        bump_author(stats, author, activity_at=ts, reviews_submitted=1)
        year = extract_year(ts)
        if year is not None:
            bump_author(yearly, f"{author}\0{year}", activity_at=ts, reviews_submitted=1)

    for row in mirror.execute("""
        SELECT author, created_at FROM pr_review_comments
        WHERE author IS NOT NULL AND author != ''
    """):
        author, created_at = row
        ts = created_at or ""
        bump_author(stats, author, activity_at=ts, review_comments=1)
        year = extract_year(ts)
        if year is not None:
            bump_author(yearly, f"{author}\0{year}", activity_at=ts, review_comments=1)

    yearly_by_author: dict[str, list[dict[str, int | str]]] = {}
    for yearly_key, row in yearly.items():
        author, year_text = yearly_key.split("\0", 1)
        year = int(year_text)
        final_row = finalize_contributor_row(row)
        yearly_by_author.setdefault(author, []).append({"year": year, **final_row})

        pres.execute("""
            INSERT INTO contributor_yearly (
                author, year, commit_count, additions, deletions,
                issues_opened, prs_opened,
                issue_comments, pr_comments, reviews_submitted, review_comments,
                total_items, total_comments, activity_score,
                first_active_at, last_active_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            author,
            year,
            final_row["commit_count"],
            final_row["additions"],
            final_row["deletions"],
            final_row["issues_opened"],
            final_row["prs_opened"],
            final_row["issue_comments"],
            final_row["pr_comments"],
            final_row["reviews_submitted"],
            final_row["review_comments"],
            final_row["total_items"],
            final_row["total_comments"],
            final_row["activity_score"],
            final_row["first_active_at"],
            final_row["last_active_at"],
        ))

    for author, row in sorted(stats.items()):
        final_row = finalize_contributor_row(row)
        author_years = yearly_by_author.get(author, [])
        top_year = None
        top_year_activity = 0
        top_year_commits = 0
        if author_years:
            best = max(author_years, key=lambda item: (
                int(item["activity_score"]),
                int(item["commit_count"]),
                int(item["total_items"]),
                int(item["total_comments"]),
                int(item["year"]),
            ))
            top_year = int(best["year"])
            top_year_activity = int(best["activity_score"])
            top_year_commits = int(best["commit_count"])

        final_row["primary_role"] = infer_primary_role(final_row)
        final_row["summary"] = summarize_contributor(author, final_row)
        pres.execute("""
            INSERT INTO contributor_stats (
                author, commit_count, additions, deletions,
                issues_opened, prs_opened,
                issue_comments, pr_comments, reviews_submitted, review_comments,
                total_items, total_comments, activity_score,
                first_active_at, last_active_at,
                top_year, top_year_commits, top_year_activity,
                primary_role, summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            author,
            final_row["commit_count"],
            final_row["additions"],
            final_row["deletions"],
            final_row["issues_opened"],
            final_row["prs_opened"],
            final_row["issue_comments"],
            final_row["pr_comments"],
            final_row["reviews_submitted"],
            final_row["review_comments"],
            final_row["total_items"],
            final_row["total_comments"],
            final_row["activity_score"],
            final_row["first_active_at"],
            final_row["last_active_at"],
            top_year,
            top_year_commits,
            top_year_activity,
            final_row["primary_role"],
            final_row["summary"],
        ))

    pres.commit()
    return len(stats)


def build_reference_points(pres: sqlite3.Connection) -> int:
    """Build canonical ranked reference rows for common repository questions."""
    count = 0

    def insert_ranked(metric: str, scope: str, rows: list[tuple], detail_fn) -> None:
        nonlocal count
        for rank, row in enumerate(rows[:10], start=1):
            entity_key, label, value_int, secondary_value = row
            details = json.dumps(detail_fn(row), sort_keys=True)
            pres.execute("""
                INSERT INTO reference_points (
                    category, metric, scope, rank,
                    entity_type, entity_key, label,
                    value_int, secondary_value, details
                )
                VALUES ('contributor', ?, ?, ?, 'author', ?, ?, ?, ?, ?)
            """, (
                metric,
                scope,
                rank,
                entity_key,
                label,
                int(value_int or 0),
                int(secondary_value or 0),
                details,
            ))
            count += 1

    metric_queries = [
        (
            "commits",
            """
            SELECT author, author, commit_count, additions
            FROM contributor_stats
            WHERE commit_count > 0
            ORDER BY commit_count DESC, additions DESC, author
            """,
            lambda row: {"commit_count": row[2], "additions": row[3]},
        ),
        (
            "code_churn",
            """
            SELECT author, author, (additions + deletions) AS churn, commit_count
            FROM contributor_stats
            WHERE commit_count > 0
            ORDER BY churn DESC, commit_count DESC, author
            """,
            lambda row: {"code_churn": row[2], "commit_count": row[3]},
        ),
        (
            "discussion",
            """
            SELECT author, author, (total_comments + reviews_submitted) AS discussion_score, total_comments
            FROM contributor_stats
            WHERE total_comments > 0 OR reviews_submitted > 0
            ORDER BY discussion_score DESC, total_comments DESC, review_comments DESC, author
            """,
            lambda row: {"discussion_score": row[2], "total_comments": row[3]},
        ),
        (
            "issues_opened",
            """
            SELECT author, author, issues_opened, activity_score
            FROM contributor_stats
            WHERE issues_opened > 0
            ORDER BY issues_opened DESC, activity_score DESC, author
            """,
            lambda row: {"issues_opened": row[2], "activity_score": row[3]},
        ),
        (
            "prs_opened",
            """
            SELECT author, author, prs_opened, activity_score
            FROM contributor_stats
            WHERE prs_opened > 0
            ORDER BY prs_opened DESC, activity_score DESC, author
            """,
            lambda row: {"prs_opened": row[2], "activity_score": row[3]},
        ),
        (
            "reviews",
            """
            SELECT author, author, reviews_submitted, review_comments
            FROM contributor_stats
            WHERE reviews_submitted > 0 OR review_comments > 0
            ORDER BY reviews_submitted DESC, review_comments DESC, author
            """,
            lambda row: {"reviews_submitted": row[2], "review_comments": row[3]},
        ),
        (
            "overall_activity",
            """
            SELECT author, author, activity_score, commit_count
            FROM contributor_stats
            WHERE activity_score > 0
            ORDER BY activity_score DESC, commit_count DESC, total_items DESC, author
            """,
            lambda row: {"activity_score": row[2], "commit_count": row[3]},
        ),
    ]

    for metric, sql, detail_fn in metric_queries:
        rows = pres.execute(sql).fetchall()
        insert_ranked(metric, "all_time", rows, detail_fn)

    for (year,) in pres.execute("SELECT DISTINCT year FROM contributor_yearly ORDER BY year"):
        rows = pres.execute("""
            SELECT author, author, activity_score, commit_count
            FROM contributor_yearly
            WHERE year = ? AND activity_score > 0
            ORDER BY activity_score DESC, commit_count DESC, total_items DESC, author
        """, (year,)).fetchall()
        insert_ranked(
            "overall_activity",
            f"year:{year}",
            rows,
            lambda row, yr=year: {"year": yr, "activity_score": row[2], "commit_count": row[3]},
        )

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
    # page_size already set to 1024 at creation, just VACUUM to compact
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

    n_contributors = build_contributor_stats(mirror, pres, author_map)
    print(f"  Contributors: {n_contributors}")

    n_reference_points = build_reference_points(pres)
    print(f"  Reference points: {n_reference_points}")

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
    pres.execute("INSERT OR REPLACE INTO meta VALUES ('total_contributors', ?)", (str(n_contributors),))
    pres.execute("INSERT OR REPLACE INTO meta VALUES ('total_reference_points', ?)", (str(n_reference_points),))
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
