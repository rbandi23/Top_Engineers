"""Streamlit dashboard for PostHog Engineer Impact scores."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from posthog_impact.scoring import parse_prs, score_engineers

PROCESSED_DIR = Path(__file__).resolve().parent.parent / "data" / "processed"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


# ── Data loading (cached) ──────────────────────────────────────────────────

@st.cache_data
def _load_latest_scores() -> tuple[list[dict], dict] | None:
    """Load the most recent scores JSON file. Returns (scores, metadata)."""
    files = sorted(PROCESSED_DIR.glob("scores_*.json"))
    if not files:
        return None
    data = json.loads(files[-1].read_text())
    return data.get("scores", data), data.get("_metadata", {})


@st.cache_data
def _load_raw_prs(raw_file_path: str) -> list[dict] | None:
    """Load raw PR data for live re-scoring."""
    path = Path(raw_file_path)
    if not path.exists():
        # Try relative to project root
        path = RAW_DIR / path.name
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _rescore(raw_prs: list[dict], exclude_noisy: bool) -> list[dict]:
    """Re-run the scoring pipeline in memory."""
    prs = parse_prs(raw_prs, exclude_noisy=exclude_noisy)
    scores = score_engineers(prs)
    return [
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
    ]


# ── Dashboard ───────────────────────────────────────────────────────────────

def main() -> None:
    """Render the Streamlit dashboard."""
    st.set_page_config(
        page_title="PostHog Engineer Impact",
        layout="wide",
    )
    loaded = _load_latest_scores()
    if loaded is None:
        st.error(
            "No score data found. Run the pipeline first:\n\n"
            "```bash\n"
            "python scripts/fetch.py\n"
            "python scripts/score.py\n"
            "```"
        )
        return

    scores_data, metadata = loaded

    pr_count = metadata.get("pr_count", "?")
    engineer_count = metadata.get("engineer_count", len(scores_data))
    computed_at = metadata.get("computed_at", "")
    if computed_at:
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(computed_at)
            computed_label = dt.strftime("%b %d, %Y")
        except ValueError:
            computed_label = computed_at
    else:
        computed_label = "unknown"

    # ── Compact header ───────────────────────────────────────────────────
    col_h1, col_h2 = st.columns([3, 2])
    with col_h1:
        st.markdown("## PostHog Engineer Impact Dashboard")
        st.caption("Shipping velocity · review contribution · codebase centrality")
    with col_h2:
        st.caption(
            f"{engineer_count} engineers · {pr_count} merged PRs · "
            f"last 90 days · computed {computed_label}"
        )

    # ── Sidebar filters ─────────────────────────────────────────────────
    with st.sidebar:
        st.header("Filters")

        top_n = st.slider("Top N engineers", min_value=3, max_value=20, value=5)

        exclude_noisy = st.toggle(
            "Exclude noisy files (lockfiles, snapshots, etc.)", value=True,
            help="Toggling re-scores all engineers from raw data. "
                 "Noisy files include lockfiles, snapshots, and generated code.",
        )

        # Live re-scoring when noisy toggle changes
        raw_file = metadata.get("raw_file", "")
        if not exclude_noisy and raw_file:
            raw_prs = _load_raw_prs(raw_file)
            if raw_prs is not None:
                scores_data = _rescore(raw_prs, exclude_noisy=False)

    df = pd.DataFrame(scores_data)
    if df.empty:
        st.warning("No engineers found in the scores data.")
        return

    min_impact = st.sidebar.slider(
        "Minimum Impact Score",
        min_value=0.0,
        max_value=float(df["final_impact"].max()),
        value=0.0,
        step=1.0,
    )

    # ── Filter ──────────────────────────────────────────────────────────
    _BOT_PATTERNS = ("bot", "[bot]", "-app", "dependabot", "copilot-swe", "posthog-bot")
    is_bot = df["login"].str.lower().apply(
        lambda x: any(p in x for p in _BOT_PATTERNS)
    )
    filtered = df[~is_bot & (df["final_impact"] >= min_impact)].head(top_n)
    if filtered.empty:
        st.warning("No engineers match the current filters.")
        return

    # ── Side-by-side: table (left) + chart (right) ──────────────────────
    display_df = filtered[
        [
            "login",
            "final_impact",
            "total_shipping",
            "total_reviews",
            "core_touch_ratio",
            "active_weeks",
            "pr_count",
            "review_count",
        ]
    ].copy()
    display_df["core_touch_ratio"] = display_df["core_touch_ratio"].apply(
        lambda x: f"{x:.0%}"
    )
    display_df["active_weeks"] = display_df["active_weeks"].apply(
        lambda w: f"{w} / 13"
    )
    display_df = display_df.rename(columns={
        "login": "Engineer",
        "final_impact": "Impact Score",
        "total_shipping": "Shipping",
        "total_reviews": "Reviews",
        "core_touch_ratio": "Core Ratio",
        "active_weeks": "Weeks Active",
        "pr_count": "PRs Merged",
        "review_count": "Reviews Given",
    })
    display_df = display_df.reset_index(drop=True)
    display_df.index = display_df.index + 1

    col_table, col_chart = st.columns([3, 2])

    with col_table:
        st.markdown(f"**Top {len(filtered)} Engineers**")
        st.dataframe(
            display_df.style.format({
                "Impact Score": "{:.1f}",
                "Shipping": "{:.1f}",
                "Reviews": "{:.1f}",
            }),
            use_container_width=True,
            height=215,
        )

    with col_chart:
        st.markdown("**Shipping vs Review Contribution**")
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=filtered["login"],
            y=filtered["total_shipping"],
            name="Shipping",
            marker_color="#FF6B6B",
        ))
        fig.add_trace(go.Bar(
            x=filtered["login"],
            y=filtered["total_reviews"],
            name="Reviews",
            marker_color="#4ECDC4",
        ))
        fig.update_layout(
            barmode="stack",
            xaxis_title="Engineer",
            yaxis_title="Log-scaled score",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=10, b=40, l=50, r=10),
            height=215,
        )
        st.plotly_chart(fig, use_container_width=True)

    # ── Top PRs (collapsed) ──────────────────────────────────────────────
    with st.expander("Top PRs by Engineer"):
        for _, row in filtered.iterrows():
            login = row["login"]
            top_prs = row.get("top_prs", [])
            if not top_prs:
                continue
            st.markdown(f"**{login}**")
            for pr_info in top_prs:
                complexity_val = pr_info.get("complexity", 0)
                discussion_val = pr_info.get("discussion", 0)
                reason = (
                    f"complexity={complexity_val:.1f}"
                    if complexity_val >= discussion_val
                    else f"discussion={discussion_val:.1f}"
                )
                st.markdown(
                    f"- [#{pr_info['number']}: {pr_info['title']}]({pr_info['url']}) "
                    f"— Shipping={pr_info['pr_shipping']:.1f} ({reason})"
                )

    # ── Scoring methodology ──────────────────────────────────────────────
    with st.expander("How Scoring Works"):
        st.markdown("""
**Impact Score** = BaseImpact × CoreMultiplier × ConsistencyBonus

| Component | Formula | Range |
|---|---|---|
| **BaseImpact** | 0.65 × Shipping + 0.35 × Reviews | 0 – ∞ (log-scaled, unbounded) |
| **CoreMultiplier** | 1 + 0.3 × CoreRatio | **1.0x** – **1.3x** |
| **ConsistencyBonus** | 1 + 0.2 × (WeeksActive / 12) | **1.0x** – **1.2x** |

**Column definitions:**

- **Shipping** — Sum of `log(1 + changed_files) + 0.6 × log(1 + lines_changed) + 0.3 × log(1 + comments + review_threads)` across all merged PRs by this engineer. Larger, more-discussed PRs score higher, but logarithmic scaling prevents mega-PRs from dominating.
- **Reviews** — Sum of `complexity(reviewed_PR) × (1 + 0.05 × log(1 + comments_left))` for each PR reviewed. Reviewing complex code scores more; leaving substantive comments adds a small bonus. Self-reviews are excluded. Only the latest review per PR counts (deduplicated).
- **Core Ratio** — Fraction of this engineer's code changes (by lines) that land in "core" directories (the smallest set of top-level dirs covering 80% of all repo activity). Range: 0% – 100%.
- **Weeks Active** — Number of distinct calendar weeks (out of the last 13) with at least one merged PR or submitted review. Measures sustained contribution vs. one-off bursts.
- **PRs Merged** — Count of merged pull requests authored by this engineer.
- **Reviews Given** — Count of distinct (PR, reviewer) review events (deduplicated).

**Data notes:** Lockfiles, snapshots, and generated code are excluded by default. PR file lists from the GitHub API may be truncated for large PRs; complexity always uses PR-level totals (not file-list sums) to avoid undercounting.
""")
    st.caption(
        "Impact Score = BaseImpact (0.65 × Shipping + 0.35 × Reviews) "
        "× CoreMultiplier × ConsistencyBonus"
    )


if __name__ == "__main__":
    main()
