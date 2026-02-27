"""Scoring engine for the Engineer Impact Dashboard.

Implements the full scoring model:
    FinalImpact = BaseImpact * CoreMultiplier * ConsistencyBonus

Where:
    BaseImpact  = 0.65 * TotalShipping + 0.35 * TotalReviews
    CoreMultiplier = 1 + 0.3 * core_touch_ratio  (if touches >= threshold)
    ConsistencyBonus = 1 + 0.2 * (active_weeks / 12)

Key invariants:
    - PR complexity always uses PR-level totals (changed_files_count,
      additions + deletions), never file-list counts which may be truncated.
    - Reviews are deduped per (PR, reviewer): only the latest review counts.
    - Core-dir touches scale per-directory churn to match PR-level totals,
      compensating for truncated file lists.
"""

from __future__ import annotations

import fnmatch
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from posthog_impact.config import (
    COMPLEXITY_CHURN_COEFF,
    CONSISTENCY_BOOST,
    CONSISTENCY_WEEKS,
    CORE_COVERAGE_THRESHOLD,
    CORE_MULTIPLIER_BOOST,
    DISCUSSION_COEFF,
    MIN_WEIGHTED_TOUCHES,
    NOISY_FILE_PATTERNS,
    REVIEW_COMMENT_COEFF,
    REVIEW_WEIGHT,
    SHIPPING_WEIGHT,
)
from posthog_impact.models import EngineerScore, FileChange, PullRequest, Review

logger = logging.getLogger(__name__)


# ── Noisy-file filtering ───────────────────────────────────────────────────

def _is_noisy(path: str) -> bool:
    """Return True if *path* matches any noisy file pattern."""
    basename = path.split("/")[-1]
    for pattern in NOISY_FILE_PATTERNS:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
    return False


# ── Review deduplication ────────────────────────────────────────────────────

def _dedupe_reviews(reviews: list[Review]) -> list[Review]:
    """Keep only the latest review per reviewer on a single PR.

    If someone left multiple reviews (e.g. COMMENTED then APPROVED),
    only the most recent one counts.  Comment counts from earlier
    reviews are summed into the kept review so nothing is lost.
    """
    by_author: dict[str, list[Review]] = defaultdict(list)
    for rev in reviews:
        by_author[rev.author_login].append(rev)

    deduped: list[Review] = []
    for _login, author_reviews in by_author.items():
        author_reviews.sort(key=lambda r: r.submitted_at)
        latest = author_reviews[-1]
        # Sum comment counts from all reviews by this author on this PR
        total_comments = sum(r.comment_count for r in author_reviews)
        deduped.append(Review(
            author_login=latest.author_login,
            state=latest.state,
            submitted_at=latest.submitted_at,
            comment_count=total_comments,
        ))

    return deduped


# ── Parsing raw API data ───────────────────────────────────────────────────

def parse_prs(raw_prs: list[dict], exclude_noisy: bool = True) -> list[PullRequest]:
    """Convert raw GraphQL JSON dicts into ``PullRequest`` model objects.

    Reviews are deduped per (PR, reviewer) — only the latest review per
    reviewer counts, with comment counts summed across all their reviews.

    Review ``comment_count`` defaults to 0 when missing from the API
    response — scoring never errors on absent fields.
    """
    result: list[PullRequest] = []

    for raw in raw_prs:
        author = raw.get("author") or {}
        login = author.get("login", "ghost")

        raw_reviews: list[Review] = []
        for r in (raw.get("reviews", {}).get("nodes") or []):
            r_author = r.get("author") or {}
            submitted = r.get("submittedAt")
            raw_reviews.append(Review(
                author_login=r_author.get("login", "ghost"),
                state=r.get("state", ""),
                submitted_at=(
                    datetime.fromisoformat(submitted.replace("Z", "+00:00"))
                    if submitted
                    else datetime.now(timezone.utc)
                ),
                comment_count=(r.get("comments") or {}).get("totalCount", 0),
            ))

        reviews = _dedupe_reviews(raw_reviews)

        files: list[FileChange] = []
        for f in raw.get("_files") or []:
            if exclude_noisy and _is_noisy(f["path"]):
                continue
            files.append(FileChange(
                path=f["path"],
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
            ))

        merged_at_str = raw.get("mergedAt", "")
        created_at_str = raw.get("createdAt", "")

        pr = PullRequest(
            node_id=raw.get("id", ""),
            number=raw.get("number", 0),
            title=raw.get("title", ""),
            url=raw.get("url", ""),
            author_login=login,
            merged_at=(
                datetime.fromisoformat(merged_at_str.replace("Z", "+00:00"))
                if merged_at_str
                else datetime.now(timezone.utc)
            ),
            created_at=(
                datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_at_str
                else datetime.now(timezone.utc)
            ),
            changed_files_count=raw.get("changedFiles", 0),
            additions=raw.get("additions", 0),
            deletions=raw.get("deletions", 0),
            comments_total=(raw.get("comments") or {}).get("totalCount", 0),
            review_threads_total=(raw.get("reviewThreads") or {}).get("totalCount", 0),
            reviews=reviews,
            files=files,
        )
        result.append(pr)

    return result


# ── Per-PR scoring helpers ──────────────────────────────────────────────────
# Complexity ALWAYS uses PR-level totals (changed_files_count, additions +
# deletions).  File lists from the API are often truncated for large PRs,
# so len(pr.files) or summed file churn would undercount.

def pr_complexity(pr: PullRequest) -> float:
    """``log1p(changed_files_count) + 0.6 * log1p(additions + deletions)``

    Uses PR-level metadata, never the file list (which may be truncated).
    """
    return (
        math.log1p(pr.changed_files_count)
        + COMPLEXITY_CHURN_COEFF * math.log1p(pr.additions + pr.deletions)
    )


def pr_discussion(pr: PullRequest) -> float:
    """``0.3 * log1p(comments_total + review_threads_total)``"""
    return DISCUSSION_COEFF * math.log1p(
        pr.comments_total + pr.review_threads_total
    )


def pr_shipping(pr: PullRequest) -> float:
    """``PRShipping = complexity + discussion``"""
    return pr_complexity(pr) + pr_discussion(pr)


def review_points(pr: PullRequest, reviewer_comment_count: int) -> float:
    """``complexity(pr) * (1 + 0.05 * log1p(comment_count))``"""
    return pr_complexity(pr) * (
        1 + REVIEW_COMMENT_COEFF * math.log1p(reviewer_comment_count)
    )


# ── Core directory computation ──────────────────────────────────────────────
# File data may be incomplete (truncated by the API).  We scale per-directory
# churn proportionally so it sums to the PR-level total churn, preventing
# big PRs from being unfairly undervalued in core scoring.

def _scaled_dir_churn(pr: PullRequest) -> dict[str, float]:
    """Compute per-directory churn for a PR, scaled to PR-level totals.

    If the file list is empty, returns an empty dict (the PR is skipped
    in core-touch computation).  If the file list covers less churn than
    the PR-level total, each directory's churn is scaled up proportionally.
    """
    if not pr.files:
        return {}

    dir_churn_raw: dict[str, int] = defaultdict(int)
    for f in pr.files:
        dir_churn_raw[f.directory] += f.churn

    file_level_total = sum(dir_churn_raw.values())
    pr_level_total = pr.additions + pr.deletions

    if file_level_total <= 0:
        return {}

    scale = pr_level_total / file_level_total if pr_level_total > file_level_total else 1.0

    return {d: churn * scale for d, churn in dir_churn_raw.items()}


def compute_core_dirs(prs: list[PullRequest]) -> set[str]:
    """Identify the smallest set of top-level dirs covering 80% of activity.

    ``dir_score[d] = sum(log1p(scaled_churn_in_d_per_PR))`` over all PRs.
    PRs with no file data are skipped.
    """
    dir_score: dict[str, float] = defaultdict(float)

    for pr in prs:
        for d, churn in _scaled_dir_churn(pr).items():
            dir_score[d] += math.log1p(churn)

    if not dir_score:
        return set()

    sorted_dirs = sorted(dir_score.items(), key=lambda x: x[1], reverse=True)
    total = sum(v for _, v in sorted_dirs)
    if total == 0:
        return set()

    cumulative = 0.0
    core: set[str] = set()
    for d, score in sorted_dirs:
        cumulative += score
        core.add(d)
        if cumulative / total >= CORE_COVERAGE_THRESHOLD:
            break

    logger.info("Core directories (%d): %s", len(core), sorted(core))
    return core


def engineer_core_touch_ratio(
    authored_prs: list[PullRequest],
    core_dirs: set[str],
) -> float:
    """Ratio of core-weighted touches to total-weighted touches.

    Uses scaled directory churn (matching PR-level totals).
    PRs with no file data are skipped.
    Returns 0.0 if total weighted touches < ``MIN_WEIGHTED_TOUCHES``.
    """
    total_wt = 0.0
    core_wt = 0.0

    for pr in authored_prs:
        for d, churn in _scaled_dir_churn(pr).items():
            w = math.log1p(churn)
            total_wt += w
            if d in core_dirs:
                core_wt += w

    if total_wt < MIN_WEIGHTED_TOUCHES:
        return 0.0
    return core_wt / total_wt


# ── Consistency ─────────────────────────────────────────────────────────────

def compute_active_weeks(
    authored_prs: list[PullRequest],
    reviewed: list[tuple[PullRequest, Review]],
) -> int:
    """Count distinct ISO weeks with a merged PR or submitted review."""
    weeks: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=CONSISTENCY_WEEKS)

    for pr in authored_prs:
        if pr.merged_at >= cutoff:
            weeks.add(pr.merged_at.strftime("%G-%V"))

    for _pr, rev in reviewed:
        if rev.submitted_at >= cutoff:
            weeks.add(rev.submitted_at.strftime("%G-%V"))

    return len(weeks)


# ── Main scoring pipeline ──────────────────────────────────────────────────

def score_engineers(prs: list[PullRequest]) -> list[EngineerScore]:
    """Compute ``FinalImpact`` for every engineer and return sorted results.

    Self-reviews (author reviewing their own PR) are excluded.
    Reviews are already deduped per (PR, reviewer) during parsing.
    """
    prs_by_author: dict[str, list[PullRequest]] = defaultdict(list)
    reviews_by_reviewer: dict[str, list[tuple[PullRequest, Review]]] = defaultdict(list)

    for pr in prs:
        prs_by_author[pr.author_login].append(pr)
        for rev in pr.reviews:
            if rev.author_login != pr.author_login:
                reviews_by_reviewer[rev.author_login].append((pr, rev))

    all_engineers = set(prs_by_author.keys()) | set(reviews_by_reviewer.keys())
    core_dirs = compute_core_dirs(prs)

    results: list[EngineerScore] = []

    for login in all_engineers:
        authored = prs_by_author.get(login, [])
        reviewed = reviews_by_reviewer.get(login, [])

        # A) BaseImpact
        total_shipping = sum(pr_shipping(pr) for pr in authored)
        if total_shipping <= 0:
            # Exclude engineers with no shipping contribution
            continue
        total_review_pts = sum(
            review_points(pr, rev.comment_count) for pr, rev in reviewed
        )
        base_impact = (
            SHIPPING_WEIGHT * total_shipping + REVIEW_WEIGHT * total_review_pts
        )

        # B) CoreMultiplier
        ctr = engineer_core_touch_ratio(authored, core_dirs)
        core_mult = 1 + CORE_MULTIPLIER_BOOST * ctr

        # C) ConsistencyBonus
        active_wks = compute_active_weeks(authored, reviewed)
        consistency = 1 + CONSISTENCY_BOOST * (active_wks / CONSISTENCY_WEEKS)

        # FinalImpact
        final = base_impact * core_mult * consistency

        # Top 3 PRs by shipping score
        authored_scored = sorted(
            [
                {
                    "number": pr.number,
                    "title": pr.title,
                    "url": pr.url,
                    "pr_shipping": round(pr_shipping(pr), 2),
                    "complexity": round(pr_complexity(pr), 2),
                    "discussion": round(pr_discussion(pr), 2),
                }
                for pr in authored
            ],
            key=lambda x: x["pr_shipping"],
            reverse=True,
        )

        results.append(EngineerScore(
            login=login,
            final_impact=round(final, 2),
            total_shipping=round(total_shipping, 2),
            total_reviews=round(total_review_pts, 2),
            base_impact=round(base_impact, 2),
            core_touch_ratio=round(ctr, 3),
            core_multiplier=round(core_mult, 3),
            active_weeks=active_wks,
            consistency_bonus=round(consistency, 3),
            pr_count=len(authored),
            review_count=len(reviewed),
            top_prs=authored_scored[:3],
        ))

    results.sort(key=lambda s: s.final_impact, reverse=True)
    return results
