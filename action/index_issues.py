"""Sync GitHub issues into the mirror database using GraphQL.

Wave 1: Metadata + bodies (from list, newest first)
Wave 2: Comments (for items that have them, newest first)

Supports incremental sync via updated_at tracking.

Usage:
  python action/index_issues.py --repo fastled/FastLED --db mirror.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

from github_graphql import GitHubGraphQL
from mirror_schema import create_mirror_db

# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

ISSUES_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(
      first: 100,
      orderBy: {field: UPDATED_AT, direction: DESC},
      after: $cursor
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        state
        stateReason
        author { login }
        body
        createdAt
        updatedAt
        closedAt
        locked
        labels(first: 20) { nodes { name } }
        comments { totalCount }
        reactions { totalCount }
      }
    }
  }
}
"""

# Batch fetch comments for up to 10 issues at once using aliases
COMMENTS_BATCH_TEMPLATE = """
query($owner: String!, $name: String!) {{
  repository(owner: $owner, name: $name) {{
    {aliases}
  }}
}}
"""

COMMENT_ALIAS = """
    i{number}: issue(number: {number}) {{
      comments(first: 100) {{
        nodes {{
          databaseId
          author {{ login }}
          body
          createdAt
          updatedAt
        }}
      }}
    }}
"""


# ---------------------------------------------------------------------------
# Wave 1: Sync metadata + bodies
# ---------------------------------------------------------------------------

def sync_issues_wave1(
    gh: GitHubGraphQL,
    conn: sqlite3.Connection,
    owner: str,
    name: str,
    since: str | None = None,
    max_pages: int = 100,
) -> int:
    """Fetch issue metadata + bodies via paginated GraphQL. Returns count synced."""
    now = datetime.now(timezone.utc).isoformat()
    total_synced = 0
    cursor: str | None = None

    for page in range(max_pages):
        data = gh.query(ISSUES_QUERY, {
            "owner": owner,
            "name": name,
            "cursor": cursor,
        })

        repo = data["repository"]
        issues_conn = repo["issues"]
        nodes = issues_conn["nodes"]

        if page == 0:
            print(f"  Issues total on GitHub: {issues_conn['totalCount']}")

        if not nodes:
            break

        for issue in nodes:
            # If incremental, stop when we hit items we've already synced
            if since and issue["updatedAt"] <= since:
                print(f"  Reached already-synced issue #{issue['number']} (updated {issue['updatedAt']})")
                return total_synced

            labels = [lb["name"] for lb in (issue.get("labels", {}).get("nodes", []))]
            author = (issue.get("author") or {}).get("login", "")

            conn.execute("""
                INSERT INTO issues (number, title, state, state_reason, author, body,
                    labels, created_at, updated_at, closed_at, comments_count,
                    reactions_count, locked, depth, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(number) DO UPDATE SET
                    title=excluded.title, state=excluded.state,
                    state_reason=excluded.state_reason, author=excluded.author,
                    body=excluded.body, labels=excluded.labels,
                    updated_at=excluded.updated_at, closed_at=excluded.closed_at,
                    comments_count=excluded.comments_count,
                    reactions_count=excluded.reactions_count,
                    locked=excluded.locked,
                    depth=MAX(depth, excluded.depth),
                    synced_at=excluded.synced_at
            """, (
                issue["number"],
                issue["title"],
                issue["state"],
                issue.get("stateReason"),
                author,
                issue.get("body") or "",
                json.dumps(labels),
                issue["createdAt"],
                issue["updatedAt"],
                issue.get("closedAt"),
                issue.get("comments", {}).get("totalCount", 0),
                issue.get("reactions", {}).get("totalCount", 0),
                1 if issue.get("locked") else 0,
                1,  # depth=1 (we have the body)
                now,
            ))
            total_synced += 1

        conn.commit()

        page_info = issues_conn["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

        if page % 5 == 4:
            budget = gh.budget_remaining()
            budget_str = f", API budget: {budget}" if budget is not None else ""
            print(f"  Wave 1 progress: {total_synced} issues synced ({page + 1} pages){budget_str}")

    return total_synced


# ---------------------------------------------------------------------------
# Wave 2: Sync comments
# ---------------------------------------------------------------------------

def sync_issue_comments(
    gh: GitHubGraphQL,
    conn: sqlite3.Connection,
    owner: str,
    name: str,
    batch_size: int = 10,
    max_batches: int = 200,
) -> int:
    """Fetch comments for issues that need them. Returns count of issues updated."""
    # Find issues at depth=1 (have body, need comments) with comments_count > 0
    # Prioritize by most recently updated
    rows = conn.execute("""
        SELECT number, comments_count FROM issues
        WHERE depth < 2 AND comments_count > 0
        ORDER BY updated_at DESC
    """).fetchall()

    if not rows:
        print("  No issues need comment sync")
        return 0

    print(f"  Wave 2: {len(rows)} issues need comments")
    now = datetime.now(timezone.utc).isoformat()
    total_updated = 0

    # Process in batches
    for batch_idx in range(0, min(len(rows), max_batches * batch_size), batch_size):
        batch = rows[batch_idx:batch_idx + batch_size]

        # Build aliased GraphQL query
        aliases = "".join(
            COMMENT_ALIAS.format(number=num) for num, _ in batch
        )
        query_str = COMMENTS_BATCH_TEMPLATE.format(aliases=aliases)

        try:
            data = gh.query(query_str, {"owner": owner, "name": name})
        except RuntimeError as e:
            print(f"  Warning: comment batch failed: {e}")
            continue

        repo = data.get("repository", {})
        for num, _ in batch:
            issue_data = repo.get(f"i{num}")
            if not issue_data:
                continue

            comments = issue_data.get("comments", {}).get("nodes", [])

            # Upsert comments
            for c in comments:
                c_id = c.get("databaseId")
                if not c_id:
                    continue
                c_author = (c.get("author") or {}).get("login", "")
                conn.execute("""
                    INSERT INTO issue_comments (id, issue_number, author, body, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        body=excluded.body, updated_at=excluded.updated_at
                """, (
                    c_id, num, c_author,
                    c.get("body") or "",
                    c.get("createdAt"),
                    c.get("updatedAt"),
                ))

            # Mark issue as depth=2
            conn.execute(
                "UPDATE issues SET depth = 2, synced_at = ? WHERE number = ?",
                (now, num),
            )
            total_updated += 1

        conn.commit()

        if (batch_idx // batch_size) % 5 == 4:
            budget = gh.budget_remaining()
            budget_str = f", API budget: {budget}" if budget is not None else ""
            print(f"  Wave 2 progress: {total_updated}/{len(rows)} issues with comments{budget_str}")

        # Check budget
        budget = gh.budget_remaining()
        if budget is not None and budget < 50:
            print(f"  Wave 2: stopping early, API budget low ({budget} remaining)")
            break

    return total_updated


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------

def log_sync(
    conn: sqlite3.Connection,
    entity_type: str,
    depth: int,
    items_synced: int,
    api_calls: int,
    started_at: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO sync_log (entity_type, depth, items_synced, api_calls_used, started_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (entity_type, depth, items_synced, api_calls, started_at, now))
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync GitHub issues to mirror DB")
    parser.add_argument("--repo", required=True, help="GitHub repo (owner/repo)")
    parser.add_argument("--db", default="mirror.db", help="Mirror database path")
    parser.add_argument("--wave", type=int, default=0, help="Max wave to run (0=all, 1=metadata only, 2=+comments)")
    args = parser.parse_args()

    owner, name = args.repo.split("/", 1)

    conn = create_mirror_db(args.db)
    gh = GitHubGraphQL()

    started = datetime.now(timezone.utc).isoformat()
    calls_before = gh.calls_made

    # Check for incremental: what's the latest updated_at we've seen?
    row = conn.execute("SELECT MAX(updated_at) FROM issues").fetchone()
    since = row[0] if row and row[0] else None
    if since:
        print(f"Incremental sync: issues updated after {since}")
    else:
        print("Full sync: no existing issues in mirror")

    # Wave 1: metadata + bodies
    print("Wave 1: syncing issue metadata + bodies...")
    n1 = sync_issues_wave1(gh, conn, owner, name, since=since)
    print(f"Wave 1 complete: {n1} issues synced ({gh.calls_made - calls_before} API calls)")
    log_sync(conn, "issues", 1, n1, gh.calls_made - calls_before, started)

    if args.wave == 1:
        print("Stopping after wave 1 (--wave 1)")
        conn.close()
        return

    # Wave 2: comments
    calls_before_w2 = gh.calls_made
    started_w2 = datetime.now(timezone.utc).isoformat()
    print("\nWave 2: syncing issue comments...")
    n2 = sync_issue_comments(gh, conn, owner, name)
    print(f"Wave 2 complete: {n2} issues updated with comments ({gh.calls_made - calls_before_w2} API calls)")
    log_sync(conn, "issue_comments", 2, n2, gh.calls_made - calls_before_w2, started_w2)

    # Summary
    total_issues = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    depth_counts = conn.execute(
        "SELECT depth, COUNT(*) FROM issues GROUP BY depth ORDER BY depth"
    ).fetchall()
    print(f"\nMirror DB: {total_issues} issues total")
    for depth, count in depth_counts:
        print(f"  depth {depth}: {count}")
    print(f"Total API calls: {gh.calls_made}")

    conn.close()


if __name__ == "__main__":
    main()
