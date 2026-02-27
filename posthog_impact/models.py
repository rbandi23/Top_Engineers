"""Domain models for the Engineer Impact Dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FileChange:
    """A single file touched in a pull request."""

    path: str
    additions: int
    deletions: int

    @property
    def churn(self) -> int:
        """Total lines changed (additions + deletions)."""
        return self.additions + self.deletions

    @property
    def directory(self) -> str:
        """Top-level directory, or ``'.'`` for root-level files."""
        parts = self.path.split("/")
        return parts[0] if len(parts) > 1 else "."


@dataclass
class Review:
    """A review left by an engineer on a pull request."""

    author_login: str
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED
    submitted_at: datetime
    comment_count: int = 0


@dataclass
class PullRequest:
    """A merged pull request with its reviews and file changes."""

    node_id: str
    number: int
    title: str
    url: str
    author_login: str
    merged_at: datetime
    created_at: datetime
    changed_files_count: int
    additions: int
    deletions: int
    comments_total: int
    review_threads_total: int
    reviews: list[Review] = field(default_factory=list)
    files: list[FileChange] = field(default_factory=list)

    @property
    def total_churn(self) -> int:
        """Sum of churn from file list; falls back to PR-level stats."""
        if self.files:
            return sum(f.churn for f in self.files)
        return self.additions + self.deletions


@dataclass
class EngineerScore:
    """Computed impact score for one engineer."""

    login: str
    final_impact: float
    total_shipping: float
    total_reviews: float
    base_impact: float
    core_touch_ratio: float
    core_multiplier: float
    active_weeks: int
    consistency_bonus: float
    pr_count: int
    review_count: int
    top_prs: list[dict] = field(default_factory=list)
