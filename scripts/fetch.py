"""CLI: Fetch merged PRs from PostHog/posthog via GitHub API."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from posthog_impact.config import DEFAULT_LOOKBACK_DAYS, DEFAULT_ORG, DEFAULT_REPO, RAW_DIR
from posthog_impact.fetcher import fetch_all

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)


def main() -> None:
    """Fetch merged PRs and save to data/raw/."""
    since = datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    prs = fetch_all(DEFAULT_ORG, DEFAULT_REPO, since)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_path = RAW_DIR / f"prs_{timestamp}.json"
    out_path.write_text(json.dumps(prs, indent=2, default=str))
    logger.info("Saved %d PRs with files → %s", len(prs), out_path)


if __name__ == "__main__":
    main()
