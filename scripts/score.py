"""CLI: Compute engineer impact scores from raw PR data."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from posthog_impact.config import PROCESSED_DIR, RAW_DIR
from posthog_impact.scoring import parse_prs, score_engineers

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)


def _latest_raw_file() -> Path | None:
    """Return the most recent raw PR dump file."""
    files = sorted(RAW_DIR.glob("prs_*.json"))
    return files[-1] if files else None


def main() -> None:
    """Load raw PRs, compute scores, and save to processed/."""
    raw_file = _latest_raw_file()
    if raw_file is None:
        print("No raw data found. Run: python scripts/fetch.py")
        return

    logger.info("Loading raw data from %s", raw_file)
    raw_prs = json.loads(raw_file.read_text())

    prs = parse_prs(raw_prs, exclude_noisy=True)
    logger.info("Parsed %d PRs", len(prs))

    scores = score_engineers(prs)
    logger.info("Scored %d engineers", len(scores))

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_path = PROCESSED_DIR / f"scores_{timestamp}.json"

    serialized = {
        "_metadata": {
            "raw_file": str(raw_file),
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "pr_count": len(prs),
            "engineer_count": len(scores),
        },
        "scores": [
            {
                "login": s.login,
                "final_impact": s.final_impact,
                "total_shipping": s.total_shipping,
                "total_reviews": s.total_reviews,
                "base_impact": s.base_impact,
                "core_touch_ratio": s.core_touch_ratio,
                "core_multiplier": s.core_multiplier,
                "active_weeks": s.active_weeks,
                "consistency_bonus": s.consistency_bonus,
                "pr_count": s.pr_count,
                "review_count": s.review_count,
                "top_prs": s.top_prs,
            }
            for s in scores
        ],
    }

    out_path.write_text(json.dumps(serialized, indent=2))
    logger.info("Saved scores → %s", out_path)

    print(f"\nTop 10 engineers by FinalImpact (from {len(prs)} merged PRs):\n")
    for i, s in enumerate(scores[:10], 1):
        print(
            f"  {i:2d}. {s.login:<25s}  Impact={s.final_impact:8.2f}  "
            f"Ship={s.total_shipping:.1f}  Rev={s.total_reviews:.1f}  "
            f"CoreRatio={s.core_touch_ratio:.2f}  Weeks={s.active_weeks}"
        )


if __name__ == "__main__":
    main()
