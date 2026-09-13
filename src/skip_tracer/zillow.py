"""Zillow zpid-free search link builder.

Confirmed with a real example during planning: "7924 Lakenheath Way,
Potomac, MD 20854" (MD iMap ACCTID 161002621465) resolves to
zillow.com/homedetails/7924-Lakenheath-Way-Rockville-MD-20854/37267570_zpid/.
That confirms the canonical slug format (pure hyphens, no commas), but v1
has no zpid for off-market leads, so it builds the zpid-free search-format
URL (/homes/<slug>_rb/) instead — UNVERIFIED that this resolves the same
way without a zpid; see scripts/verify_zillow_slug.py.

IMPORTANT: pass PREMCITY, not CITY, from the MD iMap response. This exact
record has CITY="POTOMAC" but PREMCITY="ROCKVILLE" — a real USPS
preferred-city-name mismatch for this ZIP, not a Zillow quirk. The working
URL used "Rockville." Building the slug from CITY instead of PREMCITY would
likely have produced a URL that doesn't resolve.
"""

from __future__ import annotations

import re

_WHITESPACE_PATTERN = re.compile(r"\s+")


def zillow_search_link(street: str, premise_city: str, state: str, zipcode: str) -> str:
    slug = _WHITESPACE_PATTERN.sub("-", f"{street} {premise_city} {state} {zipcode}".strip())
    return f"https://www.zillow.com/homes/{slug}_rb/"
