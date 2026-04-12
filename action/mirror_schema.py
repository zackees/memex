"""Mirror database schema — faithful sync of GitHub data."""

from __future__ import annotations

import sqlite3

MIRROR_SCHEMA = """
-- Issues (from GitHub API)
CREATE TABLE IF NOT EXISTS issues (
    number INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    state_reason TEXT,
    author TEXT,
    body TEXT,
    labels TEXT,            -- JSON array of label names
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT,
    comments_count INTEGER DEFAULT 0,
    reactions_count INTEGER DEFAULT 0,
    locked INTEGER DEFAULT 0,
    depth INTEGER DEFAULT 0,    -- 0=stub, 1=body, 2=comments
    synced_at TEXT               -- when this row was last synced
);

-- Comments (shared by issues and PRs — GitHub uses the same number space)
CREATE TABLE IF NOT EXISTS issue_comments (
    id INTEGER PRIMARY KEY,
    issue_number INTEGER NOT NULL,  -- issue or PR number
    author TEXT,
    body TEXT,
    created_at TEXT,
    updated_at TEXT
);

-- Pull requests (from GitHub API)
CREATE TABLE IF NOT EXISTS pull_requests (
    number INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    author TEXT,
    body TEXT,
    labels TEXT,            -- JSON array of label names
    draft INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT,
    merged_at TEXT,
    merge_commit_sha TEXT,
    head_ref TEXT,
    base_ref TEXT,
    additions INTEGER,
    deletions INTEGER,
    changed_files INTEGER,
    comments_count INTEGER DEFAULT 0,
    review_comments_count INTEGER DEFAULT 0,
    commits_count INTEGER DEFAULT 0,
    depth INTEGER DEFAULT 0,    -- 0=stub, 1=body+stats, 2=comments+reviews
    synced_at TEXT
);

-- PR reviews
CREATE TABLE IF NOT EXISTS pr_reviews (
    id INTEGER PRIMARY KEY,
    pr_number INTEGER NOT NULL,
    author TEXT,
    state TEXT,             -- APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED
    body TEXT,
    submitted_at TEXT,
    FOREIGN KEY (pr_number) REFERENCES pull_requests(number)
);

-- PR review comments (inline code comments)
CREATE TABLE IF NOT EXISTS pr_review_comments (
    id INTEGER PRIMARY KEY,
    pr_number INTEGER NOT NULL,
    author TEXT,
    body TEXT,
    path TEXT,
    diff_hunk TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY (pr_number) REFERENCES pull_requests(number)
);

-- Commits (from git log, free)
CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    author_name TEXT,
    author_email TEXT,
    author_date TEXT,
    message TEXT,
    files_changed INTEGER,
    additions INTEGER,
    deletions INTEGER,
    file_stats TEXT,        -- JSON array of {path, additions, deletions}
    synced_at TEXT
);

-- Branches
CREATE TABLE IF NOT EXISTS branches (
    name TEXT PRIMARY KEY,
    commit_sha TEXT,
    protected INTEGER DEFAULT 0,
    synced_at TEXT
);

-- Releases
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,
    tag_name TEXT,
    name TEXT,
    body TEXT,
    draft INTEGER DEFAULT 0,
    prerelease INTEGER DEFAULT 0,
    author TEXT,
    created_at TEXT,
    published_at TEXT,
    synced_at TEXT
);

-- Repository metadata (single row)
CREATE TABLE IF NOT EXISTS repo_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Sync tracking: what ranges have been synced at what depth
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,   -- 'issues', 'prs', 'commits', etc.
    depth INTEGER NOT NULL,      -- depth level achieved
    range_start TEXT,            -- ISO 8601 or NULL for "all"
    range_end TEXT,              -- ISO 8601 or NULL for "all"
    items_synced INTEGER,
    api_calls_used INTEGER,
    started_at TEXT,
    completed_at TEXT
);

-- Soft-delete log: items that disappeared from API
CREATE TABLE IF NOT EXISTS tombstones (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,     -- number for issues/PRs, sha for commits
    last_seen_at TEXT,
    tombstoned_at TEXT,
    PRIMARY KEY (entity_type, entity_id)
);
"""


def create_mirror_db(db_path: str) -> sqlite3.Connection:
    """Create or open a mirror database with the full schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(MIRROR_SCHEMA)
    conn.commit()
    return conn
