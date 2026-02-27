"""Tests for the Engineer Impact Dashboard."""

from __future__ import annotations

import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from posthog_impact.models import EngineerScore, FileChange, PullRequest, Review
from posthog_impact.scoring import (
    _dedupe_reviews,
    _is_noisy,
    _scaled_dir_churn,
    compute_active_weeks,
    compute_core_dirs,
    engineer_core_touch_ratio,
    parse_prs,
    pr_complexity,
    pr_discussion,
    pr_shipping,
    review_points,
    score_engineers,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

NOW = datetime.now(timezone.utc)


def _make_pr(
    author: str = "alice",
    files: list[FileChange] | None = None,
    changed_files_count: int | None = None,
    additions: int = 100,
    deletions: int = 50,
    comments: int = 0,
    review_threads: int = 0,
    reviews: list[Review] | None = None,
    merged_at: datetime | None = None,
    number: int = 1,
) -> PullRequest:
    """Build a PullRequest for tests."""
    if files is None:
        files = [FileChange("src/index.ts", 100, 50)]
    if merged_at is None:
        merged_at = NOW
    if changed_files_count is None:
        changed_files_count = len(files) if files else 1
    return PullRequest(
        node_id=f"node_{number}",
        number=number,
        title=f"PR #{number}",
        url=f"https://github.com/PostHog/posthog/pull/{number}",
        author_login=author,
        merged_at=merged_at,
        created_at=merged_at - timedelta(days=1),
        changed_files_count=changed_files_count,
        additions=additions,
        deletions=deletions,
        comments_total=comments,
        review_threads_total=review_threads,
        reviews=reviews or [],
        files=files,
    )


# ── Smoke tests ─────────────────────────────────────────────────────────────


def test_module_entry_point() -> None:
    """``python -m posthog_impact`` exits 0 and prints version info."""
    result = subprocess.run(
        [sys.executable, "-m", "posthog_impact"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "posthog_impact" in result.stdout


def test_imports() -> None:
    """All package modules are importable."""
    from posthog_impact import __version__
    from posthog_impact.config import GRAPHQL_URL, PROJECT_ROOT, SHIPPING_WEIGHT
    from posthog_impact.fetcher import fetch_all  # noqa: F401
    from posthog_impact.github_client import GitHubClient  # noqa: F401
    from posthog_impact.models import EngineerScore, FileChange, PullRequest, Review  # noqa: F401
    from posthog_impact.scoring import score_engineers  # noqa: F401

    assert isinstance(__version__, str)
    assert PROJECT_ROOT.exists()
    assert GRAPHQL_URL.startswith("https://")
    assert 0 < SHIPPING_WEIGHT < 1


# ── Model tests ─────────────────────────────────────────────────────────────


def test_file_change_churn() -> None:
    fc = FileChange(path="src/api/handler.ts", additions=50, deletions=20)
    assert fc.churn == 70
    assert fc.directory == "src"


def test_file_change_root_directory() -> None:
    fc = FileChange(path="README.md", additions=1, deletions=0)
    assert fc.directory == "."


def test_pull_request_total_churn_from_files() -> None:
    pr = _make_pr(
        files=[FileChange("a.py", 30, 10), FileChange("b.py", 20, 5)],
        additions=100,
        deletions=50,
    )
    # total_churn uses file list when present
    assert pr.total_churn == 65


def test_pull_request_total_churn_fallback() -> None:
    pr = _make_pr(files=[], additions=200, deletions=100)
    assert pr.total_churn == 300


# ── Complexity always uses PR-level totals ──────────────────────────────────


def test_pr_complexity_uses_pr_level_totals() -> None:
    """Complexity must use changed_files_count and additions+deletions,
    not len(files) or summed file churn."""
    pr = _make_pr(
        files=[FileChange("a.py", 10, 5)],  # 1 file, 15 churn in file list
        changed_files_count=50,  # PR-level says 50 files
        additions=5000,
        deletions=2000,  # PR-level says 7000 churn
    )
    expected = math.log1p(50) + 0.6 * math.log1p(7000)
    assert abs(pr_complexity(pr) - expected) < 0.001


# ── Review deduplication ────────────────────────────────────────────────────


def test_dedupe_reviews_keeps_latest() -> None:
    """Only the latest review per author is kept; comment counts are summed."""
    r1 = Review("bob", "COMMENTED", NOW - timedelta(hours=2), comment_count=3)
    r2 = Review("bob", "APPROVED", NOW - timedelta(hours=1), comment_count=1)
    r3 = Review("carol", "APPROVED", NOW, comment_count=2)

    deduped = _dedupe_reviews([r1, r2, r3])
    assert len(deduped) == 2

    bob = next(r for r in deduped if r.author_login == "bob")
    assert bob.state == "APPROVED"
    assert bob.comment_count == 4  # 3 + 1


def test_dedupe_reviews_single_review() -> None:
    r = Review("alice", "APPROVED", NOW, comment_count=0)
    deduped = _dedupe_reviews([r])
    assert len(deduped) == 1
    assert deduped[0].author_login == "alice"


# ── Scoring formula tests ──────────────────────────────────────────────────


def test_pr_shipping_basic() -> None:
    pr = _make_pr(
        changed_files_count=1,
        additions=100,
        deletions=50,
        comments=5,
        review_threads=3,
    )
    complexity = math.log1p(1) + 0.6 * math.log1p(150)
    discussion = 0.3 * math.log1p(5 + 3)
    expected = complexity + discussion
    assert abs(pr_shipping(pr) - expected) < 0.001


def test_review_points_no_comments() -> None:
    pr = _make_pr(changed_files_count=2, additions=50, deletions=50)
    expected = pr_complexity(pr) * 1.0
    assert abs(review_points(pr, 0) - expected) < 0.001


def test_review_points_with_comments() -> None:
    pr = _make_pr(changed_files_count=2, additions=50, deletions=50)
    expected = pr_complexity(pr) * (1 + 0.05 * math.log1p(10))
    assert abs(review_points(pr, 10) - expected) < 0.001


# ── Core directory computation ──────────────────────────────────────────────


def test_scaled_dir_churn_matches_pr_totals() -> None:
    """Scaled dir churn should sum to PR-level total when files are truncated."""
    pr = _make_pr(
        files=[FileChange("frontend/app.tsx", 100, 50)],  # 150 file churn
        additions=500,
        deletions=200,  # 700 PR-level churn
    )
    scaled = _scaled_dir_churn(pr)
    assert "frontend" in scaled
    assert abs(sum(scaled.values()) - 700) < 0.01


def test_scaled_dir_churn_empty_files() -> None:
    pr = _make_pr(files=[], additions=500, deletions=200)
    assert _scaled_dir_churn(pr) == {}


def test_core_dirs_computation() -> None:
    prs = [
        _make_pr(
            files=[FileChange("frontend/app.tsx", 500, 200)],
            additions=700, deletions=0, changed_files_count=1,
        ),
        _make_pr(
            files=[FileChange("frontend/index.tsx", 300, 100)],
            additions=400, deletions=0, changed_files_count=1, number=2,
        ),
        _make_pr(
            files=[FileChange("docs/readme.md", 5, 2)],
            additions=7, deletions=0, changed_files_count=1, number=3,
        ),
    ]
    core = compute_core_dirs(prs)
    assert "frontend" in core


def test_core_touch_ratio() -> None:
    core = {"frontend", "posthog"}
    prs = [
        _make_pr(
            files=[FileChange("frontend/x.ts", 100, 50)],
            additions=150, deletions=0, changed_files_count=1,
        ),
        _make_pr(
            files=[FileChange("docs/y.md", 10, 5)],
            additions=15, deletions=0, changed_files_count=1, number=2,
        ),
    ]
    ratio = engineer_core_touch_ratio(prs, core)
    assert 0 < ratio < 1


# ── Consistency ─────────────────────────────────────────────────────────────


def test_consistency_12_weeks() -> None:
    prs = [
        _make_pr(
            author="alice",
            merged_at=NOW - timedelta(weeks=w),
            number=w + 1,
        )
        for w in range(12)
    ]
    active = compute_active_weeks(prs, [])
    assert active == 12


def test_consistency_one_week() -> None:
    prs = [_make_pr(author="alice", merged_at=NOW)]
    active = compute_active_weeks(prs, [])
    assert active == 1


# ── Noisy file filter ──────────────────────────────────────────────────────


def test_noisy_file_filter() -> None:
    assert _is_noisy("pnpm-lock.yaml") is True
    assert _is_noisy("yarn.lock") is True
    assert _is_noisy("dist/bundle.js") is True
    assert _is_noisy("foo/__snapshots__/bar.snap") is True
    assert _is_noisy("src/api/handler.ts") is False
    assert _is_noisy("frontend/src/scenes/app.tsx") is False


# ── End-to-end scoring ─────────────────────────────────────────────────────


def test_score_engineers_end_to_end() -> None:
    review_by_bob = Review("bob", "APPROVED", NOW, comment_count=3)
    prs = [
        _make_pr(
            author="alice",
            changed_files_count=5,
            additions=200,
            deletions=100,
            comments=4,
            review_threads=2,
            reviews=[review_by_bob],
        ),
    ]
    scores = score_engineers(prs)
    assert len(scores) >= 1

    alice = next((s for s in scores if s.login == "alice"), None)
    bob = next((s for s in scores if s.login == "bob"), None)

    assert alice is not None
    assert alice.final_impact > 0
    assert alice.pr_count == 1
    assert alice.total_shipping > 0

    assert bob is not None
    assert bob.review_count == 1
    assert bob.total_reviews > 0


def test_self_reviews_excluded() -> None:
    """Author reviewing their own PR should not count as a review."""
    self_review = Review("alice", "APPROVED", NOW, comment_count=1)
    prs = [
        _make_pr(
            author="alice",
            changed_files_count=3,
            additions=100,
            deletions=50,
            reviews=[self_review],
        ),
    ]
    scores = score_engineers(prs)
    alice = next(s for s in scores if s.login == "alice")
    assert alice.review_count == 0
    assert alice.total_reviews == 0.0


def test_missing_review_comment_count() -> None:
    """Review with missing comment count should default to 0, not error."""
    raw = [{
        "id": "n1",
        "number": 1,
        "title": "t",
        "url": "u",
        "author": {"login": "alice"},
        "mergedAt": NOW.isoformat(),
        "createdAt": NOW.isoformat(),
        "changedFiles": 1,
        "additions": 10,
        "deletions": 5,
        "comments": {"totalCount": 0},
        "reviewThreads": {"totalCount": 0},
        "reviews": {"nodes": [
            {"author": {"login": "bob"}, "state": "APPROVED", "submittedAt": NOW.isoformat()},
        ]},
        "_files": [],
    }]
    prs = parse_prs(raw)
    assert prs[0].reviews[0].comment_count == 0
    # Scoring should not error
    scores = score_engineers(prs)
    assert len(scores) >= 1
