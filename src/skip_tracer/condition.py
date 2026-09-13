"""v1 condition proxy — free/already-available signals only.

No source in this pipeline gives an actual repair-cost dollar figure — this
produces a rough tier, not an estimate, to help triage which leads deserve
an actual drive-by. Street View + vision-model condition reading is a
planned v2 enrichment (see the repo's GitHub issues), not built here.
"""

from __future__ import annotations

from typing import Any

RECENT_PERMIT_YEAR_THRESHOLD = 2015
UPDATED_GRADE_THRESHOLD = 7
DATED_GRADE_THRESHOLD = 4


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
