"""Manual verification script — Test-First Checklist item 3.

Prints the zpid-free search-format URL for the one confirmed real example
from planning, alongside the known-good canonical (zpid) URL it should
resolve to the same listing as. Open both in a browser and compare —
zillow_search_link() only builds the slug; it can't confirm Zillow actually
resolves it without a live check.

Usage:
    PYTHONPATH=src pipenv run python scripts/verify_zillow_slug.py
"""

from __future__ import annotations

from skip_tracer.zillow import zillow_search_link

KNOWN_GOOD_CANONICAL_URL = (
    "https://www.zillow.com/homedetails/"
    "7924-Lakenheath-Way-Rockville-MD-20854/37267570_zpid/"
)


def main() -> None:
    search_url = zillow_search_link("7924 Lakenheath Way", "Rockville", "MD", "20854")
    print(f"zpid-free search URL to test: {search_url}")
    print(f"Known-good canonical URL to compare against: {KNOWN_GOOD_CANONICAL_URL}")
    print(
        "\nOpen the search URL in a browser. It should land on (or redirect "
        "to) the same listing as the canonical URL above."
    )


if __name__ == "__main__":
    main()
