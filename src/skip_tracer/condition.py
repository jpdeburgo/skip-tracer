"""v1 condition proxy — free/already-available signals only.

No source in this pipeline gives an actual repair-cost dollar figure from an
inspection or contractor quote. estimate_repair_cost() below is a rough
industry rule-of-thumb (a flat $/sqft by condition tier), not a real
estimate — it only produces a number when condition_tier() has enough
signal to place a property in likely-updated/likely-dated; "unassessed"
means there's no basis for even a rule-of-thumb guess, so it returns None
rather than fabricate one. Street View + vision-model condition reading is
a planned v2 enrichment (see the repo's GitHub issues), not built here.
"""

from __future__ import annotations

from typing import Any

RECENT_PERMIT_YEAR_THRESHOLD = 2015
UPDATED_GRADE_THRESHOLD = 7
DATED_GRADE_THRESHOLD = 4

# Flat $/sqft rule-of-thumb rehab cost by condition_tier, loosely based on
# common light-cosmetic vs. moderate-rehab investor ballparks. No tier for
# "unassessed" is intentional — there's no signal to even guess from.
REPAIR_COST_PER_SQFT = {
    "likely-updated": 12.0,
    "likely-dated": 40.0,
}


def condition_tier(record: dict[str, Any], permit_history: list[dict[str, Any]]) -> str:
    """Combines MD iMap's STRUGRAD (1-9 construction-quality grade, set at
    assessment) with BatchData permit recency into one of:
    likely-updated / likely-dated / unassessed.
    """
    grade = record.get("STRUGRAD")
    recent_permits = [
        permit
        for permit in permit_history
        if permit.get("year", 0) >= RECENT_PERMIT_YEAR_THRESHOLD
    ]

    if grade and int(grade) >= UPDATED_GRADE_THRESHOLD and recent_permits:
        return "likely-updated"
    if grade and int(grade) <= DATED_GRADE_THRESHOLD and not recent_permits:
        return "likely-dated"
    return "unassessed"


def estimate_repair_cost(tier: str, building_sqft: float | None) -> float | None:
    """Rough $/sqft rule-of-thumb repair cost for a condition_tier() result —
    not a contractor quote. Returns None when there's no square footage or
    the tier itself carries no signal ("unassessed")."""
    if not building_sqft or tier not in REPAIR_COST_PER_SQFT:
        return None
    return REPAIR_COST_PER_SQFT[tier] * building_sqft
