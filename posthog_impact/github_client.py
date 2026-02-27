"""GitHub API client supporting both REST and GraphQL with rate-limit handling."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from posthog_impact.config import (
    GITHUB_API_BASE,
    GITHUB_TOKEN,
    GRAPHQL_URL,
    RATE_LIMIT_BUFFER,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    RETRY_MAX,
)

logger = logging.getLogger(__name__)


class GitHubClient:
    """Unified GitHub client for REST and GraphQL with automatic retries."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or GITHUB_TOKEN
        if not self._token:
            raise ValueError(
                "GITHUB_TOKEN is required. Set it as an environment variable."
            )
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        self._remaining: int = 5000
        self._reset_at: float = 0.0

    # ── GraphQL ─────────────────────────────────────────────────────────

    def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query. Must include ``rateLimit`` selection.

        Returns the ``data`` dict from the response.
        """
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        for attempt in range(1, RETRY_MAX + 1):
            self._wait_if_rate_limited()

            try:
                resp = self._client.post(GRAPHQL_URL, json=payload)
            except httpx.TransportError as exc:
                logger.warning("Transport error (attempt %d/%d): %s", attempt, RETRY_MAX, exc)
                if attempt == RETRY_MAX:
                    raise RuntimeError(f"All {RETRY_MAX} retries exhausted") from exc
                time.sleep(RETRY_BACKOFF ** attempt)
                continue

            if resp.status_code in (403, 429):
                self._handle_rate_limit_response(resp, attempt)
                continue

            resp.raise_for_status()
            body = resp.json()

            # Track rate limit from GraphQL response body
            rate_info = body.get("data", {}).get("rateLimit")
            if rate_info:
                self._update_rate_limit(rate_info)

            if "errors" in body:
                error_msg = "; ".join(
                    e.get("message", str(e)) for e in body["errors"]
                )
                if "rate limit" in error_msg.lower():
                    logger.warning("GraphQL rate-limit error, sleeping 60s")
                    time.sleep(60)
                    continue
                raise RuntimeError(f"GraphQL errors: {error_msg}")

            return body["data"]

        raise RuntimeError(f"All {RETRY_MAX} retries exhausted")

    # ── REST ────────────────────────────────────────────────────────────

    def rest_get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send a GET request to the GitHub REST API.

        Returns parsed JSON. Handles rate-limit headers and retries.
        """
        url = f"{GITHUB_API_BASE}{endpoint}"

        for attempt in range(1, RETRY_MAX + 1):
            self._wait_if_rate_limited()

            try:
                resp = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                logger.warning("Transport error (attempt %d/%d): %s", attempt, RETRY_MAX, exc)
                if attempt == RETRY_MAX:
                    raise RuntimeError(f"All {RETRY_MAX} retries exhausted") from exc
                time.sleep(RETRY_BACKOFF ** attempt)
                continue

            # Track rate limit from REST headers
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset_ts = resp.headers.get("X-RateLimit-Reset")
            if remaining is not None:
                self._remaining = int(remaining)
            if reset_ts is not None:
                self._reset_at = float(reset_ts)

            if resp.status_code in (403, 429):
                self._handle_rate_limit_response(resp, attempt)
                continue

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"All {RETRY_MAX} retries exhausted")

    # ── Rate-limit helpers ──────────────────────────────────────────────

    def _wait_if_rate_limited(self) -> None:
        """Sleep if remaining API points are below the safety buffer."""
        if self._remaining < RATE_LIMIT_BUFFER:
            now = time.time()
            wait = max(0, self._reset_at - now) + 5
            if wait > 0:
                logger.info(
                    "Rate limit low (%d remaining). Sleeping %.0fs.",
                    self._remaining,
                    wait,
                )
                time.sleep(wait)

    def _handle_rate_limit_response(self, resp: httpx.Response, attempt: int) -> None:
        """Handle an HTTP 403/429 rate-limit response."""
        retry_after = int(resp.headers.get("Retry-After", "60"))
        logger.warning(
            "Rate limited (HTTP %d). Sleeping %ds (attempt %d/%d).",
            resp.status_code,
            retry_after,
            attempt,
            RETRY_MAX,
        )
        time.sleep(retry_after)

    def _update_rate_limit(self, rate_info: dict[str, Any]) -> None:
        """Update internal rate-limit state from a GraphQL rateLimit field."""
        self._remaining = rate_info.get("remaining", self._remaining)
        reset_at_str = rate_info.get("resetAt")
        if reset_at_str:
            reset_dt = datetime.fromisoformat(
                reset_at_str.replace("Z", "+00:00")
            )
            self._reset_at = reset_dt.timestamp()
        logger.debug(
            "Rate limit: cost=%s remaining=%s",
            rate_info.get("cost"),
            self._remaining,
        )

    # ── Context manager ─────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
