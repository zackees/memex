"""Test search quality against the presentation database.

Runs a suite of queries and reports precision metrics.
Each test case has a query, expected entity type, and keywords
that SHOULD appear in the top results.

Usage:
  python action/test_search.py --db index.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass


@dataclass
class TestCase:
    name: str
    query: str
    index: str  # 'porter' or 'trigram'
    entity_filter: str | None  # 'issue', 'pr', 'commit', or None for all
    expected_keywords: list[str]  # keywords that should appear in top results
    top_k: int = 10


# ---------------------------------------------------------------------------
# Test cases designed for FastLED repository
# ---------------------------------------------------------------------------

TESTS: list[TestCase] = [
    # --- Porter (natural language) on items ---
    TestCase(
        name="ESP32 issues",
        query="ESP32 support",
        index="porter",
        entity_filter="issue",
        expected_keywords=["ESP32", "esp32"],
    ),
    TestCase(
        name="LED color problems",
        query="color wrong pixel",
        index="porter",
        entity_filter=None,
        expected_keywords=["color", "pixel", "Color", "Pixel"],
    ),
    TestCase(
        name="Memory leak",
        query="memory OR leak OR crash",
        index="porter",
        entity_filter="issue",
        expected_keywords=["memory", "leak", "crash", "Memory"],
    ),
    TestCase(
        name="WS2812 driver",
        query="WS2812 driver timing",
        index="porter",
        entity_filter=None,
        expected_keywords=["WS2812", "ws2812", "WS2811", "timing"],
    ),
    TestCase(
        name="SPI output",
        query="SPI output pin",
        index="porter",
        entity_filter=None,
        expected_keywords=["SPI", "spi", "output", "pin"],
    ),
    TestCase(
        name="Brightness control",
        query="brightness fade dim",
        index="porter",
        entity_filter=None,
        expected_keywords=["brightness", "Brightness", "fade", "dim"],
    ),
    TestCase(
        name="Audio reactive",
        query="audio reactive FFT",
        index="porter",
        entity_filter=None,
        expected_keywords=["audio", "Audio", "reactive", "FFT"],
    ),

    # --- Trigram (substring) on items ---
    TestCase(
        name="Trigram: FastLED",
        query='"FastLED"',
        index="trigram",
        entity_filter=None,
        expected_keywords=["FastLED"],
    ),
    TestCase(
        name="Trigram: CRGB",
        query='"CRGB"',
        index="trigram",
        entity_filter=None,
        expected_keywords=["CRGB"],
    ),
    TestCase(
        name="Trigram: RMT",
        query='"RMT"',
        index="trigram",
        entity_filter="issue",
        expected_keywords=["RMT", "rmt"],
    ),

    # --- Porter on commits ---
    TestCase(
        name="Commit: fix compilation",
        query="fix compilation error",
        index="porter_commits",
        entity_filter=None,
        expected_keywords=["fix", "Fix", "compil", "error"],
    ),
    TestCase(
        name="Commit: ESP32 support",
        query="ESP32 support add",
        index="porter_commits",
        entity_filter=None,
        expected_keywords=["ESP32", "esp32"],
    ),
    TestCase(
        name="Commit: refactor",
        query="refactor OR clean OR rename",
        index="porter_commits",
        entity_filter=None,
        expected_keywords=["refactor", "Refactor", "clean", "rename", "Rename"],
    ),

    # --- Faceted search ---
    TestCase(
        name="Open issues about ESP32",
        query="ESP32",
        index="porter_faceted_open",
        entity_filter="issue",
        expected_keywords=["ESP32", "esp32"],
    ),
    TestCase(
        name="Merged PRs",
        query="fix bug",
        index="porter_faceted_merged",
        entity_filter="pr",
        expected_keywords=["fix", "Fix", "bug", "Bug"],
    ),

    # --- Cross-entity (unified) ---
    TestCase(
        name="Unified: WLED",
        query="WLED",
        index="porter_unified",
        entity_filter=None,
        expected_keywords=["WLED"],
    ),

    # --- Comment search ---
    TestCase(
        name="Comment: workaround",
        query="workaround solution",
        index="porter_comments",
        entity_filter=None,
        expected_keywords=["workaround", "Workaround", "solution"],
    ),
]


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def run_items_porter(conn: sqlite3.Connection, query: str, entity_filter: str | None,
                     state_filter: str | None = None, top_k: int = 10) -> list[dict]:
    """Search items with porter FTS + BM25 + recency."""
    sql = """
        SELECT e.number, e.entity_type, e.title, e.state, e.author, e.updated_at,
            e.comment_count, e.ref_count,
            bm25(items_fts, 10.0, 1.0, 5.0, 3.0) AS text_rank,
            snippet(items_fts, 1, '>>>', '<<<', '...', 32) AS snippet
        FROM items_fts f
        JOIN items e ON e.id = f.rowid
        WHERE items_fts MATCH ?
    """
    params: list = [query]
    if entity_filter:
        sql += " AND e.entity_type = ?"
        params.append(entity_filter)
    if state_filter:
        sql += " AND e.state = ?"
        params.append(state_filter)
    sql += " ORDER BY text_rank LIMIT ?"
    params.append(top_k)

    rows = conn.execute(sql, params).fetchall()
    return [{"number": r[0], "type": r[1], "title": r[2], "state": r[3],
             "author": r[4], "updated": r[5], "comments": r[6], "refs": r[7],
             "rank": r[8], "snippet": r[9]} for r in rows]


def run_items_trigram(conn: sqlite3.Connection, query: str, entity_filter: str | None,
                      top_k: int = 10) -> list[dict]:
    sql = """
        SELECT e.number, e.entity_type, e.title, e.state,
            bm25(items_trigram, 10.0, 1.0, 5.0, 3.0) AS rank,
            snippet(items_trigram, 1, '>>>', '<<<', '...', 32) AS snippet
        FROM items_trigram f
        JOIN items e ON e.id = f.rowid
        WHERE items_trigram MATCH ?
    """
    params: list = [query]
    if entity_filter:
        sql += " AND e.entity_type = ?"
        params.append(entity_filter)
    sql += " ORDER BY rank LIMIT ?"
    params.append(top_k)

    rows = conn.execute(sql, params).fetchall()
    return [{"number": r[0], "type": r[1], "title": r[2], "state": r[3],
             "rank": r[4], "snippet": r[5]} for r in rows]


def run_commits_porter(conn: sqlite3.Connection, query: str, top_k: int = 10) -> list[dict]:
    sql = """
        SELECT c.sha, c.subject, c.author, c.committed_at,
            bm25(commits_fts, 10.0, 1.0, 3.0, 5.0) AS rank,
            snippet(commits_fts, 1, '>>>', '<<<', '...', 32) AS snippet
        FROM commits_fts f
        JOIN commits c ON c.id = f.rowid
        WHERE commits_fts MATCH ?
        ORDER BY rank LIMIT ?
    """
    rows = conn.execute(sql, [query, top_k]).fetchall()
    return [{"sha": r[0][:8], "subject": r[1], "author": r[2], "date": r[3],
             "rank": r[4], "snippet": r[5]} for r in rows]


def run_comments_porter(conn: sqlite3.Connection, query: str, top_k: int = 10) -> list[dict]:
    sql = """
        SELECT cm.parent_type, cm.parent_number, cm.author, cm.created_at,
            bm25(comments_fts, 3.0, 1.0) AS rank,
            snippet(comments_fts, 1, '>>>', '<<<', '...', 32) AS snippet
        FROM comments_fts f
        JOIN comments cm ON cm.id = f.rowid
        WHERE comments_fts MATCH ?
        ORDER BY rank LIMIT ?
    """
    rows = conn.execute(sql, [query, top_k]).fetchall()
    return [{"parent_type": r[0], "parent_number": r[1], "author": r[2],
             "date": r[3], "rank": r[4], "snippet": r[5]} for r in rows]


def run_unified(conn: sqlite3.Connection, query: str, top_k: int = 10) -> list[dict]:
    """Search across items + commits with normalized scores."""
    sql = """
        WITH ranked AS (
            SELECT 'item' AS source, e.entity_type AS type, e.number AS id,
                   e.title AS title,
                   snippet(items_fts, 1, '>>>', '<<<', '...', 32) AS snippet,
                   bm25(items_fts, 10.0, 1.0, 5.0, 3.0) AS score
            FROM items_fts f JOIN items e ON e.id = f.rowid
            WHERE items_fts MATCH ?

            UNION ALL

            SELECT 'commit', 'commit', c.id,
                   c.subject,
                   snippet(commits_fts, 1, '>>>', '<<<', '...', 32),
                   bm25(commits_fts, 10.0, 1.0, 3.0, 5.0)
            FROM commits_fts f JOIN commits c ON c.id = f.rowid
            WHERE commits_fts MATCH ?
        )
        SELECT * FROM ranked ORDER BY score LIMIT ?
    """
    rows = conn.execute(sql, [query, query, top_k]).fetchall()
    return [{"source": r[0], "type": r[1], "id": r[2], "title": r[3],
             "snippet": r[4], "score": r[5]} for r in rows]


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_test(conn: sqlite3.Connection, test: TestCase) -> tuple[bool, int, int, list[dict]]:
    """Run a test case. Returns (passed, hits, total_results, results)."""
    if test.index == "porter":
        results = run_items_porter(conn, test.query, test.entity_filter, top_k=test.top_k)
    elif test.index == "trigram":
        results = run_items_trigram(conn, test.query, test.entity_filter, top_k=test.top_k)
    elif test.index == "porter_commits":
        results = run_commits_porter(conn, test.query, top_k=test.top_k)
    elif test.index == "porter_comments":
        results = run_comments_porter(conn, test.query, top_k=test.top_k)
    elif test.index == "porter_faceted_open":
        results = run_items_porter(conn, test.query, test.entity_filter,
                                   state_filter="open", top_k=test.top_k)
    elif test.index == "porter_faceted_merged":
        results = run_items_porter(conn, test.query, test.entity_filter,
                                   state_filter="merged", top_k=test.top_k)
    elif test.index == "porter_unified":
        results = run_unified(conn, test.query, top_k=test.top_k)
    else:
        results = []

    # Check: do any expected keywords appear in the top results?
    all_text = ""
    for r in results:
        for v in r.values():
            all_text += str(v) + " "

    hits = sum(1 for kw in test.expected_keywords if kw in all_text)
    passed = hits > 0 and len(results) > 0

    return passed, hits, len(results), results


def main() -> None:
    parser = argparse.ArgumentParser(description="Test search quality")
    parser.add_argument("--db", required=True, help="Presentation database")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full results")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    total_tests = len(TESTS)
    passed_tests = 0
    total_keyword_hits = 0
    total_keywords = 0

    print(f"Running {total_tests} search quality tests...\n")

    for test in TESTS:
        passed, hits, n_results, results = run_test(conn, test)
        total_keywords += len(test.expected_keywords)
        total_keyword_hits += hits

        status = "PASS" if passed else "FAIL"
        if passed:
            passed_tests += 1

        print(f"  [{status}] {test.name}")
        print(f"         query: {test.query}")
        print(f"         index: {test.index}, filter: {test.entity_filter}")
        print(f"         results: {n_results}, keyword hits: {hits}/{len(test.expected_keywords)}")

        if args.verbose and results:
            for i, r in enumerate(results[:5]):
                title = r.get("title") or r.get("subject") or ""
                print(f"           #{i+1}: {title[:70]}", flush=True)
                if r.get("snippet"):
                    snip = r["snippet"].replace("\n", " ")[:100]
                    # Safe print for Windows
                    try:
                        print(f"                {snip}", flush=True)
                    except UnicodeEncodeError:
                        print(f"                {snip.encode('ascii', 'replace').decode()}", flush=True)
        print()

    # Summary
    precision = total_keyword_hits / total_keywords * 100 if total_keywords else 0
    pass_rate = passed_tests / total_tests * 100

    print("=" * 60)
    print(f"Results: {passed_tests}/{total_tests} tests passed ({pass_rate:.0f}%)")
    print(f"Keyword precision: {total_keyword_hits}/{total_keywords} ({precision:.0f}%)")
    print("=" * 60)

    conn.close()
    sys.exit(0 if passed_tests == total_tests else 1)


if __name__ == "__main__":
    main()
