"""Minimal GitHub GraphQL client with rate-limit awareness."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from typing import Any

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


def get_token() -> str:
    """Get GitHub token from environment."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if not token:
        print("ERROR: Set GITHUB_TOKEN or GH_TOKEN environment variable", file=sys.stderr)
        sys.exit(1)
    return token


class GitHubGraphQL:
    """Simple GitHub GraphQL client with automatic rate-limit handling."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or get_token()
        self.calls_made = 0
        self.rate_remaining: int | None = None
        self.rate_reset_at: float | None = None

    def query(self, graphql: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GraphQL query. Returns the 'data' dict."""
        body = json.dumps({"query": graphql, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            GITHUB_GRAPHQL_URL,
            data=body,
            headers={
                "Authorization": f"bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "memex-indexer",
            },
        )

        # Back off if we're near the rate limit
        if self.rate_remaining is not None and self.rate_remaining < 10:
            if self.rate_reset_at:
                wait = max(0, self.rate_reset_at - time.time()) + 1
                print(f"  Rate limit nearly exhausted ({self.rate_remaining} left), waiting {wait:.0f}s...")
                time.sleep(wait)

        resp = urllib.request.urlopen(req, timeout=30)
        self.calls_made += 1

        # Track rate limit from headers
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            self.rate_remaining = int(remaining)
        if reset is not None:
            self.rate_reset_at = float(reset)

        result = json.loads(resp.read())

        if "errors" in result:
            errors = result["errors"]
            msgs = [e.get("message", str(e)) for e in errors]
            raise RuntimeError(f"GraphQL errors: {'; '.join(msgs)}")

        return result.get("data", {})

    def paginate(
        self,
        graphql_template: str,
        path: list[str],
        variables: dict[str, Any] | None = None,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Paginate a GraphQL query that uses $cursor variable.

        graphql_template must use $cursor: String variable and include
        pageInfo { hasNextPage endCursor } at the pagination level.

        path: list of keys to reach the connection, e.g. ["repository", "issues"]
        """
        all_nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        vs = dict(variables or {})

        for _ in range(max_pages):
            vs["cursor"] = cursor
            data = self.query(graphql_template, vs)

            # Navigate to the connection
            obj = data
            for key in path:
                obj = obj[key]

            nodes = obj.get("nodes", [])
            all_nodes.extend(nodes)

            page_info = obj.get("pageInfo", {})
            if not page_info.get("hasNextPage", False):
                break
            cursor = page_info.get("endCursor")

        return all_nodes

    def budget_remaining(self) -> int | None:
        """Return remaining API budget, or None if unknown."""
        return self.rate_remaining
