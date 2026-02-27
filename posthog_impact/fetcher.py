"""Three-phase data fetcher: Search → PR details → file changes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from posthog_impact.config import (
    DEFAULT_ORG,
    DEFAULT_REPO,
    FILE_PAGE_SIZE,
    REVIEW_PAGE_SIZE,
    SEARCH_PER_PAGE,
    SEARCH_WINDOW_DAYS,
)
from posthog_impact.github_client import GitHubClient

logger = logging.getLogger(__name__)

# ── GraphQL Queries ─────────────────────────────────────────────────────────

QUERY_PR_DETAILS = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      number
      title
      url
      author { login }
      mergedAt
      createdAt
      changedFiles
      additions
      deletions
      comments { totalCount }
      reviewThreads { totalCount }
      reviews(first: %d) {
        nodes {
          author { login }
          state
          submittedAt
          comments { totalCount }
        }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
""" % REVIEW_PAGE_SIZE

QUERY_FILES = """
query($nodeId: ID!, $cursor: String) {
  node(id: $nodeId) {
    ... on PullRequest {
      files(first: %d, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          path
          additions
          deletions
        }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
""" % FILE_PAGE_SIZE


# ── Phase 0: Search (REST) ─────────────────────────────────────────────────

def search_merged_pr_numbers(
    client: GitHubClient,
    owner: str,
    name: str,
    since: datetime,
    until: datetime,
) -> list[int]:
    """Search for merged PR numbers in a single date window.

    Uses the GitHub Search REST API with ``merged:`` date filter.
    Paginates through all results (100 per page, max 1000 per query).
    """
    since_str = since.strftime("%Y-%m-%d")
    until_str = until.strftime("%Y-%m-%d")
    q = f"repo:{owner}/{name} is:pr is:merged merged:{since_str}..{until_str}"

    numbers: list[int] = []
    page = 1

    while True:
        data = client.rest_get(
            "/search/issues",
            params={"q": q, "per_page": SEARCH_PER_PAGE, "page": page},
        )
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            pr_number = item.get("number")
            if pr_number is not None:
                numbers.append(pr_number)

        total_count = data.get("total_count", 0)
        if page * SEARCH_PER_PAGE >= total_count:
            break
        if page * SEARCH_PER_PAGE >= 1000:
            logger.warning(
                "Search window %s..%s hit 1000-result cap (%d total). "
                "Consider a smaller SEARCH_WINDOW_DAYS.",
                since_str,
                until_str,
                total_count,
            )
            break
        page += 1

    return numbers


def search_all_windows(
    client: GitHubClient,
    owner: str,
    name: str,
    since: datetime,
    window_days: int = SEARCH_WINDOW_DAYS,
) -> list[int]:
    """Split the date range into windows and collect all merged PR numbers.

    Returns a deduplicated, sorted list of PR numbers.
    """
    now = datetime.now(timezone.utc)
    all_numbers: set[int] = set()

    window_start = since
    window_idx = 0
    while window_start < now:
        window_end = min(window_start + timedelta(days=window_days), now)
        window_idx += 1
        logger.info(
            "Search window %d: %s → %s",
            window_idx,
            window_start.strftime("%Y-%m-%d"),
            window_end.strftime("%Y-%m-%d"),
        )

        numbers = search_merged_pr_numbers(client, owner, name, window_start, window_end)
        all_numbers.update(numbers)
        logger.info("  Found %d PRs (total unique so far: %d)", len(numbers), len(all_numbers))

        window_start = window_end

    return sorted(all_numbers)


# ── Phase 1: PR Details (GraphQL) ──────────────────────────────────────────

def fetch_pr_details(
    client: GitHubClient,
    owner: str,
    name: str,
    number: int,
) -> dict | None:
    """Fetch full metadata and reviews for a single PR by number."""
    data = client.graphql(
        QUERY_PR_DETAILS,
        variables={"owner": owner, "name": name, "number": number},
    )
    pr_data = data.get("repository", {}).get("pullRequest")
    if pr_data is None:
        logger.warning("PR #%d not found or inaccessible.", number)
    return pr_data


def fetch_all_pr_details(
    client: GitHubClient,
    owner: str,
    name: str,
    numbers: list[int],
) -> list[dict]:
    """Fetch details for all PR numbers. Logs progress."""
    results: list[dict] = []
    total = len(numbers)

    for i, number in enumerate(numbers, 1):
        if i % 50 == 1 or i == total:
            logger.info("Fetching PR details: %d/%d", i, total)
        pr = fetch_pr_details(client, owner, name, number)
        if pr is not None:
            results.append(pr)

    return results


# ── Phase 2: Files (GraphQL) ───────────────────────────────────────────────

def fetch_files_for_pr(client: GitHubClient, node_id: str) -> list[dict]:
    """Fetch all changed files for a PR using its node ID."""
    all_files: list[dict] = []
    cursor: str | None = None

    while True:
        data = client.graphql(
            QUERY_FILES,
            variables={"nodeId": node_id, "cursor": cursor},
        )
        file_conn = data["node"]["files"]
        all_files.extend(file_conn["nodes"])

        if not file_conn["pageInfo"]["hasNextPage"]:
            break
        cursor = file_conn["pageInfo"]["endCursor"]

    return all_files


# ── Orchestrator ────────────────────────────────────────────────────────────

def fetch_all(
    owner: str = DEFAULT_ORG,
    name: str = DEFAULT_REPO,
    since: datetime | None = None,
    lookback_days: int = 90,
) -> list[dict]:
    """Run the full three-phase fetch pipeline.

    1. Search REST API → merged PR numbers (7-day windows)
    2. GraphQL → PR details + reviews
    3. GraphQL → file changes per PR

    Returns a list of PR dicts with ``_files`` key attached.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    with GitHubClient() as client:
        # Phase 0
        logger.info("Phase 0: Searching for merged PRs since %s", since.strftime("%Y-%m-%d"))
        pr_numbers = search_all_windows(client, owner, name, since)
        logger.info("Phase 0 complete: %d unique PR numbers found.", len(pr_numbers))

        # Phase 1
        logger.info("Phase 1: Fetching PR details via GraphQL...")
        prs = fetch_all_pr_details(client, owner, name, pr_numbers)
        logger.info("Phase 1 complete: %d PRs fetched.", len(prs))

        # Phase 2
        logger.info("Phase 2: Fetching files for each PR...")
        for i, pr in enumerate(prs, 1):
            node_id = pr["id"]
            if i % 50 == 1 or i == len(prs):
                logger.info("Fetching files: %d/%d", i, len(prs))
            pr["_files"] = fetch_files_for_pr(client, node_id)

        logger.info("Phase 2 complete. All data fetched.")

    return prs
