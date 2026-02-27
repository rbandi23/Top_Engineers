"""Entry-point for ``python -m posthog_impact``."""

from __future__ import annotations

import sys

from posthog_impact import __version__


def main() -> None:
    """Print a short help message and exit."""
    print(
        f"posthog_impact v{__version__}\n"
        "\n"
        "Engineer Impact Dashboard for PostHog/posthog\n"
        "\n"
        "Usage:\n"
        "  python -m posthog_impact              Show this help message\n"
        "  python scripts/fetch.py               Fetch merged PRs via GitHub API\n"
        "  python scripts/score.py               Compute engineer impact scores\n"
        "  streamlit run app/streamlit_app.py     Launch the dashboard\n"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
