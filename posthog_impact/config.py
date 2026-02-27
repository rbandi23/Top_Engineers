"""Centralised configuration and constants."""

from __future__ import annotations

import os
from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────────────────
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"
PROCESSED_DIR: Path = DATA_DIR / "processed"

# ── GitHub API ──────────────────────────────────────────────────────────────
GITHUB_TOKEN: str | None = os.getenv("GITHUB_TOKEN")
GITHUB_API_BASE: str = "https://api.github.com"
GRAPHQL_URL: str = "https://api.github.com/graphql"

# ── Repository target ──────────────────────────────────────────────────────
DEFAULT_ORG: str = "PostHog"
DEFAULT_REPO: str = "posthog"
REQUEST_TIMEOUT: int = 30  # seconds

# ── Scoring weights ────────────────────────────────────────────────────────
SHIPPING_WEIGHT: float = 0.65
REVIEW_WEIGHT: float = 0.35
COMPLEXITY_CHURN_COEFF: float = 0.6
DISCUSSION_COEFF: float = 0.3
REVIEW_COMMENT_COEFF: float = 0.05
CORE_MULTIPLIER_BOOST: float = 0.3
CONSISTENCY_BOOST: float = 0.2
CORE_COVERAGE_THRESHOLD: float = 0.80
MIN_WEIGHTED_TOUCHES: float = 1.0

# ── Noisy file patterns (excluded from directory-touch computation) ─────
NOISY_FILE_PATTERNS: list[str] = [
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "*.snap",
    "*.generated.*",
    "*.min.js",
    "*.min.css",
    "dist/*",
    "build/*",
    "*.map",
    "__generated__/*",
]

# ── Fetch settings ─────────────────────────────────────────────────────────
SEARCH_WINDOW_DAYS: int = 7
SEARCH_PER_PAGE: int = 100
REVIEW_PAGE_SIZE: int = 100
FILE_PAGE_SIZE: int = 100
RATE_LIMIT_BUFFER: int = 5
RETRY_MAX: int = 3
RETRY_BACKOFF: float = 2.0  # seconds, exponential base

# ── Dashboard defaults ─────────────────────────────────────────────────────
DEFAULT_LOOKBACK_DAYS: int = 90
DEFAULT_TOP_N: int = 5
CONSISTENCY_WEEKS: int = 12
