#!/usr/bin/env python3
"""Build a tiny SQLite database for browser smoke tests."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: build-smoke-db.py <output-db>")

    output = Path(sys.argv[1]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    conn = sqlite3.connect(output)
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=OFF;

            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                number INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                state TEXT NOT NULL,
                author TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                comment_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE VIRTUAL TABLE items_fts USING fts5(
                entity_type,
                title,
                body,
                author,
                content='items',
                content_rowid='id',
                tokenize='porter unicode61'
            );

            CREATE TABLE contributor_stats (
                author TEXT PRIMARY KEY,
                summary TEXT NOT NULL
            );

            CREATE TABLE reference_points (
                category TEXT NOT NULL,
                metric TEXT NOT NULL,
                scope TEXT NOT NULL,
                rank INTEGER NOT NULL,
                entity_key TEXT NOT NULL,
                label TEXT NOT NULL,
                value_int INTEGER NOT NULL,
                secondary_value INTEGER NOT NULL
            );
            """
        )

        meta_rows = [
            ("repo", "zackees/memex"),
            ("total_items", "3"),
            ("total_commits", "12"),
            ("total_contributors", "2"),
            ("total_reference_points", "3"),
        ]
        conn.executemany("INSERT INTO meta(key, value) VALUES(?, ?)", meta_rows)

        item_rows = [
            (
                1,
                101,
                "issue",
                "ESP32 search support",
                "Investigate ESP32 indexing and search quality in the presentation database.",
                "open",
                "alice",
                "2026-04-14T00:00:00Z",
                4,
            ),
            (
                2,
                102,
                "pull_request",
                "Crash handling cleanup",
                "Tighten crash reporting paths and panic summaries for browser queries.",
                "closed",
                "bob",
                "2026-04-13T00:00:00Z",
                2,
            ),
            (
                3,
                103,
                "issue",
                "Schema inspection",
                "Expose schema metadata for wasm clients without hand-written SQL.",
                "open",
                "alice",
                "2026-04-12T00:00:00Z",
                1,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO items(
                id, number, entity_type, title, body, state, author, updated_at, comment_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            item_rows,
        )
        conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")

        contributor_rows = [
            ("alice", "Alice owns the search and schema work."),
            ("bob", "Bob focuses on crash cleanup and review."),
        ]
        conn.executemany(
            "INSERT INTO contributor_stats(author, summary) VALUES(?, ?)",
            contributor_rows,
        )

        reference_rows = [
            ("contributor", "commits", "all_time", 1, "alice", "Alice", 8, 120),
            ("contributor", "discussion", "all_time", 1, "bob", "Bob", 5, 14),
            ("contributor", "overall_activity", "all_time", 1, "alice", "Alice", 13, 8),
        ]
        conn.executemany(
            """
            INSERT INTO reference_points(
                category, metric, scope, rank, entity_key, label, value_int, secondary_value
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            reference_rows,
        )

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
