"""Sync GitHub pull requests into the mirror database using GraphQL.

Wave 1: Metadata + bodies + merge stats (from list, newest first)
Wave 2: Reviews + review comments (batched, newest first)

Supports incremental sync via updated_at tracking.

Usage:
  python action/index_prs.py --repo fastled/FastLED --db mirror.db
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from github_graphql import GitHubGraphQL
from mirror_schema import create_mirror_db

# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

PRS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
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
        author { login }
        body
        isDraft
        createdAt
        updatedAt
        closedAt
        mergedAt
        mergeCommit { oid }
        headRefName
        baseRefName
        additions
        deletions
        changedFiles
        commits { totalCount }
        comments { totalCount }
        reviewThreads { totalCount }
        labels(first: 20) { nodes { name } }
        reviews { totalCount }
      }
    }
  }
}
"""

# Batch fetch reviews + review comments for up to 5 PRs at once
REVIEWS_BATCH_TEMPLATE = """
query($owner: String!, $name: String!) {{
  repository(owner: $owner, name: $name) {{
    {aliases}
  }}
}}
"""

REVIEW_ALIAS = """
    pr{number}: pullRequest(number: {number}) {{
      reviews(first: 50) {{
        nodes {{
          databaseId
          author {{ login }}
          state
          body
          submittedAt
        }}
      }}
      reviewThreads(first: 100) {{
        nodes {{
          comments(first: 20) {{
            nodes {{
              databaseId
              author {{ login }}
              body
              path
              diffHunk
              createdAt
              updatedAt
            }}
          }}
        }}
      }}
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
# Wave 1: Sync metadata + bodies + merge stats
# ---------------------------------------------------------------------------

def sync_prs_wave1(
    gh: GitHubGraphQL,
    conn: Any,
    owner: str,
    name: str,
    since: str | None = None,
    max_pages: int = 100,
) -> int:
    """Fetch PR metadata + bodies + merge stats via paginated GraphQL."""
    now = datetime.now(timezone.utc).isoformat()
    total_synced = 0
    cursor: str | None = None

    for page in range(max_pages):
        data = gh.query(PRS_QUERY, {
            "owner": owner,
            "name": name,
            "cursor": cursor,
        })

        repo = data["repository"]
        prs_conn = repo["pullRequests"]
        nodes = prs_conn["nodes"]

        if page == 0:
            print(f"  PRs total on GitHub: {prs_conn['totalCount']}")

        if not nodes:
            break

        for pr in nodes:
            if since and pr["updatedAt"] <= since:
                print(f"  Reached already-synced PR #{pr['number']} (updated {pr['updatedAt']})")
                return total_synced

            labels = [lb["name"] for lb in (pr.get("labels", {}).get("nodes", []))]
            author = (pr.get("author") or {}).get("login", "")
            merge_sha = None
            if pr.get("mergeCommit"):
                merge_sha = pr["mergeCommit"].get("oid")

            conn.execute("""
                INSERT INTO pull_requests (number, title, state, author, body, labels,
                    draft, created_at, updated_at, closed_at, merged_at, merge_commit_sha,
                    head_ref, base_ref, additions, deletions, changed_files,
                    comments_count, review_comments_count, commits_count, depth, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(number) DO UPDATE SET
                    title=excluded.title, state=excluded.state, author=excluded.author,
                    body=excluded.body, labels=excluded.labels, draft=excluded.draft,
                    updated_at=excluded.updated_at, closed_at=excluded.closed_at,
                    merged_at=excluded.merged_at, merge_commit_sha=excluded.merge_commit_sha,
                    head_ref=excluded.head_ref, base_ref=excluded.base_ref,
                    additions=excluded.additions, deletions=excluded.deletions,
                    changed_files=excluded.changed_files,
                    comments_count=excluded.comments_count,
                    review_comments_count=excluded.review_comments_count,
                    commits_count=excluded.commits_count,
                    depth=MAX(depth, excluded.depth),
                    synced_at=excluded.synced_at
            """, (
                pr["number"],
                pr["title"],
                pr["state"],
                author,
                pr.get("body") or "",
                json.dumps(labels),
                1 if pr.get("isDraft") else 0,
                pr["createdAt"],
                pr["updatedAt"],
                pr.get("closedAt"),
                pr.get("mergedAt"),
                merge_sha,
                pr.get("headRefName", ""),
                pr.get("baseRefName", ""),
                pr.get("additions"),
                pr.get("deletions"),
                pr.get("changedFiles"),
                pr.get("comments", {}).get("totalCount", 0),
                pr.get("reviewThreads", {}).get("totalCount", 0),
                pr.get("commits", {}).get("totalCount", 0),
                1,  # depth=1 (body + stats)
                now,
            ))
            total_synced += 1

        conn.commit()

        page_info = prs_conn["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

        if page % 5 == 4:
            budget = gh.budget_remaining()
            budget_str = f", API budget: {budget}" if budget is not None else ""
            print(f"  Wave 1 progress: {total_synced} PRs synced ({page + 1} pages){budget_str}")

    return total_synced


# ---------------------------------------------------------------------------
# Wave 2: Sync reviews + review comments + PR comments
# ---------------------------------------------------------------------------

def sync_pr_reviews(
    gh: GitHubGraphQL,
    conn: Any,
    owner: str,
    name: str,
    batch_size: int = 5,
    max_batches: int = 200,
) -> int:
    """Fetch reviews + review comments for PRs that need them."""
    # PRs at depth<2 with reviews or comments
    rows = conn.execute("""
        SELECT number FROM pull_requests
        WHERE depth < 2 AND (comments_count > 0 OR review_comments_count > 0)
        ORDER BY updated_at DESC
    """).fetchall()

    if not rows:
        print("  No PRs need review/comment sync")
        return 0

    print(f"  Wave 2: {len(rows)} PRs need reviews/comments")
    now = datetime.now(timezone.utc).isoformat()
    total_updated = 0

    for batch_idx in range(0, min(len(rows), max_batches * batch_size), batch_size):
        batch = rows[batch_idx:batch_idx + batch_size]

        aliases = "".join(
            REVIEW_ALIAS.format(number=num) for (num,) in batch
        )
        query_str = REVIEWS_BATCH_TEMPLATE.format(aliases=aliases)

        try:
            data = gh.query(query_str, {"owner": owner, "name": name})
        except RuntimeError as e:
            print(f"  Warning: review batch failed: {e}")
            continue

        repo = data.get("repository", {})
        for (num,) in batch:
            pr_data = repo.get(f"pr{num}")
            if not pr_data:
                continue

            # Upsert reviews
            for review in pr_data.get("reviews", {}).get("nodes", []):
                r_id = review.get("databaseId")
                if not r_id:
                    continue
                r_author = (review.get("author") or {}).get("login", "")
                conn.execute("""
                    INSERT INTO pr_reviews (id, pr_number, author, state, body, submitted_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        state=excluded.state, body=excluded.body
                """, (
                    r_id, num, r_author,
                    review.get("state", ""),
                    review.get("body") or "",
                    review.get("submittedAt"),
                ))

            # Upsert review comments (from review threads)
            for thread in pr_data.get("reviewThreads", {}).get("nodes", []):
                for comment in thread.get("comments", {}).get("nodes", []):
                    c_id = comment.get("databaseId")
                    if not c_id:
                        continue
                    c_author = (comment.get("author") or {}).get("login", "")
                    conn.execute("""
                        INSERT INTO pr_review_comments
                            (id, pr_number, author, body, path, diff_hunk, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            body=excluded.body, updated_at=excluded.updated_at
                    """, (
                        c_id, num, c_author,
                        comment.get("body") or "",
                        comment.get("path") or "",
                        comment.get("diffHunk") or "",
                        comment.get("createdAt"),
                        comment.get("updatedAt"),
                    ))

            # Upsert PR-level comments (issue-style)
            for comment in pr_data.get("comments", {}).get("nodes", []):
                c_id = comment.get("databaseId")
                if not c_id:
                    continue
                c_author = (comment.get("author") or {}).get("login", "")
                # Store in issue_comments table (PRs and issues share comment space)
                conn.execute("""
                    INSERT INTO issue_comments (id, issue_number, author, body, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        body=excluded.body, updated_at=excluded.updated_at
                """, (
                    c_id, num, c_author,
                    comment.get("body") or "",
                    comment.get("createdAt"),
                    comment.get("updatedAt"),
                ))

            conn.execute(
                "UPDATE pull_requests SET depth = 2, synced_at = ? WHERE number = ?",
                (now, num),
            )
            total_updated += 1

        conn.commit()

        if (batch_idx // batch_size) % 10 == 9:
            budget = gh.budget_remaining()
            budget_str = f", API budget: {budget}" if budget is not None else ""
            print(f"  Wave 2 progress: {total_updated}/{len(rows)} PRs{budget_str}")

        budget = gh.budget_remaining()
        if budget is not None and budget < 50:
            print(f"  Wave 2: stopping early, API budget low ({budget} remaining)")
            break

    return total_updated


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------

def log_sync(conn: Any, entity_type: str, depth: int, items: int, calls: int, started: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO sync_log (entity_type, depth, items_synced, api_calls_used, started_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (entity_type, depth, items, calls, started, now))
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync GitHub PRs to mirror DB")
    parser.add_argument("--repo", required=True, help="GitHub repo (owner/repo)")
    parser.add_argument("--db", default="mirror.db", help="Mirror database path")
    parser.add_argument("--wave", type=int, default=0, help="Max wave (0=all, 1=metadata only, 2=+reviews)")
    args = parser.parse_args()

    owner, name = args.repo.split("/", 1)
    conn = create_mirror_db(args.db)
    gh = GitHubGraphQL()

    started = datetime.now(timezone.utc).isoformat()
    calls_before = gh.calls_made

    # Incremental check
    row = conn.execute("SELECT MAX(updated_at) FROM pull_requests").fetchone()
    since = row[0] if row and row[0] else None
    if since:
        print(f"Incremental sync: PRs updated after {since}")
    else:
        print("Full sync: no existing PRs in mirror")

    # Wave 1: metadata + bodies + merge stats
    print("Wave 1: syncing PR metadata + bodies + merge stats...")
    n1 = sync_prs_wave1(gh, conn, owner, name, since=since)
    print(f"Wave 1 complete: {n1} PRs synced ({gh.calls_made - calls_before} API calls)")
    log_sync(conn, "prs", 1, n1, gh.calls_made - calls_before, started)

    if args.wave == 1:
        print("Stopping after wave 1 (--wave 1)")
        conn.close()
        return

    # Wave 2: reviews + review comments
    calls_w2 = gh.calls_made
    started_w2 = datetime.now(timezone.utc).isoformat()
    print("\nWave 2: syncing PR reviews + comments...")
    n2 = sync_pr_reviews(gh, conn, owner, name)
    print(f"Wave 2 complete: {n2} PRs updated ({gh.calls_made - calls_w2} API calls)")
    log_sync(conn, "pr_reviews", 2, n2, gh.calls_made - calls_w2, started_w2)

    # Summary
    total_prs = conn.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0]
    merged = conn.execute("SELECT COUNT(*) FROM pull_requests WHERE merged_at IS NOT NULL").fetchone()[0]
    depth_counts = conn.execute(
        "SELECT depth, COUNT(*) FROM pull_requests GROUP BY depth ORDER BY depth"
    ).fetchall()
    print(f"\nMirror DB: {total_prs} PRs ({merged} merged)")
    for depth, count in depth_counts:
        print(f"  depth {depth}: {count}")
    print(f"Total API calls: {gh.calls_made}")

    conn.close()


if __name__ == "__main__":
    main()
