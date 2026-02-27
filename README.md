# PostHog Engineer Impact Dashboard

Rank the most impactful engineers in [PostHog/posthog](https://github.com/PostHog/posthog) over a rolling window, using merged PRs and code reviews from GitHub.

## Scoring model

```
FinalImpact = BaseImpact * CoreMultiplier * ConsistencyBonus
```

- **BaseImpact** = 0.65 × Shipping + 0.35 × Reviews
  - Shipping per PR = `log1p(changed_files) + 0.6 * log1p(churn) + 0.3 * log1p(comments + threads)`
  - Review points = `complexity(PR) * (1 + 0.05 * log1p(review_comments))`
  - Reviews deduped per (PR, reviewer) — only the latest review counts
- **CoreMultiplier** = `1 + 0.3 * core_touch_ratio` — data-derived core directories (top 80% of activity)
  - Per-directory churn scaled to PR-level totals to handle truncated file lists
- **ConsistencyBonus** = `1 + 0.2 * (active_weeks / 12)`

PR complexity always uses PR-level metadata (`changed_files`, `additions + deletions`), never file-list counts which may be truncated by the API.

## Quick start

```bash
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_your_token_here

# 1. Fetch merged PRs (Search API + GraphQL, ~2-5 min)
python scripts/fetch.py

# 2. Compute impact scores (~1 sec)
python scripts/score.py

# 3. Launch dashboard
streamlit run app/streamlit_app.py
```

## CLI

```bash
python -m posthog_impact   # prints help
```

## Tests

```bash
pytest tests/ -v
```

## Deployment (Streamlit Community Cloud)

1. Run `fetch.py` + `score.py` locally and commit `data/processed/scores_*.json`
2. Push the repo to GitHub
3. Deploy at [share.streamlit.io](https://share.streamlit.io) pointing to `app/streamlit_app.py`

The app reads pre-computed scores and supports live re-scoring via the "Exclude noisy files" toggle.
