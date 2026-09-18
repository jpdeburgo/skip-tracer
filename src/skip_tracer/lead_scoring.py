"""Lead scoring — which distressed owners to call first.

The user's goal: "If I call 50 people, ideally at least one will work
out." Ranking purely by estimated profit does not get you there, because
profit answers the wrong question. Profit tells you what a deal is worth
IF it closes. It says nothing about whether the owner will actually sell.

Those two things frequently point in opposite directions:

  - A free-and-clear house with $400k of equity and an owner who has no
    urgency is a fantastic deal that will never close. You will spend ten
    calls and get nowhere.
  - A house three weeks from the auction block with $15k of equity is an
    owner who MUST act, but there is no margin in it for you.

So this module scores three independent dimensions and combines them:

  profit_score         is there money in this if it closes?
  motivation_score     will this owner actually transact, and soon?
  contactability_score can we realistically reach the decision-maker?

and multiplies motivation against profit rather than merely adding it,
because a zero on either axis should sink the lead rather than be averaged
away by a strong showing on the other.

HONESTY ABOUT PROVENANCE: the weights below are a reasoned model derived
from this repo's transcript-based domain knowledge (see
.github/agents/skip-trace-advisor.md) and standard distressed-acquisition
fundamentals. They are NOT fitted to conversion data, because this
pipeline has not closed enough tracked deals to fit anything to yet. They
are deliberately expressed as named constants so they can be tuned once
real call outcomes accumulate in leads.status -- that feedback loop is the
whole point of storing call outcomes in the database. Treat the numbers as
a starting hypothesis, not a measured truth.
"""

from __future__ import annotations

import math
from typing import Any

# --------------------------------------------------------------------------
# Foreclosure-stage urgency.
#
# Ordered by how close the owner is to losing the house, because proximity
# to an irreversible deadline is the strongest motivator in distressed
# acquisition. An owner with a scheduled auction date has a hard clock; an
# owner merely flagged "preforeclosure" may be months from any real
# consequence and may still believe they will cure the default.
#
# Measured against this repo's own catalog, `preforeclosure` is an umbrella
# flag that every more-specific stage also carries, so it scores lowest on
# its own -- it is the floor, not a signal.
# --------------------------------------------------------------------------
FORECLOSURE_STAGE_SCORES = {
    "activeAuction": 100,
    "noticeOfSale": 90,
    "noticeOfLisPendens": 70,
    "noticeOfDefault": 60,
    "preforeclosure": 35,
}

# --------------------------------------------------------------------------
# Additional distress/motivation signals, scored independently of stage.
#
# `failedListing`/`expiredListing` are weighted heavily on purpose: an owner
# who already tried to sell on the open market and could not has
# demonstrated intent to sell, which is much stronger evidence than any
# inferred distress. The transcript's "5+ months on market usually means it
# cannot pass a lender's inspection" insight applies directly -- that is a
# seller who now has a real reason to prefer a fast as-is cash close.
#
# `vacant` scores high because an empty house is a pure carrying cost with
# no offsetting benefit to the owner. Note this pipeline uses BatchData's
# paid vacancy flag, not an unreliable free/USPS-derived list.
# --------------------------------------------------------------------------
MOTIVATION_SIGNAL_SCORES = {
    "failedListing": 35,
    "expiredListing": 35,
    "vacant": 30,
    "taxDefault": 25,
    "involuntaryLien": 20,
    "listedBelowMarketPrice": 20,
    "tiredLandlord": 20,
    "seniorOwner": 10,
    "mailingAddressVacant": 10,
}

# --------------------------------------------------------------------------
# Hard disqualifiers.
#
# corporateOwned/trustOwned mirrors cli._exclusion_reasons(): there is
# rarely a single motivated human decision-maker to build rapport with, and
# the transcript is explicit that these go nowhere for a one-person
# outreach operation.
#
# activeListing/pendingListing means the property is already under an
# exclusive listing agreement or contract -- calling the owner directly is
# at best a dead end and at worst a way to tortiously interfere with a
# broker's contract. This is deliberately distinct from failedListing /
# expiredListing above, which are strong positives.
# --------------------------------------------------------------------------
DISQUALIFYING_FLAGS = {
    "corporateOwned": "corporate owned (no single motivated decision-maker)",
    "trustOwned": "trust owned (no single motivated decision-maker)",
    "activeListing": "currently listed with an agent",
    "pendingListing": "listing already pending/under contract",
    "recentlySold": "recently sold (owner already exited)",
}

# Weights for the final blend. Motivation is weighted highest because a
# motivated seller with a mediocre spread still produces a deal, whereas an
# unmotivated seller with a huge spread produces nothing at all.
WEIGHT_MOTIVATION = 0.50
WEIGHT_PROFIT = 0.35
WEIGHT_CONTACTABILITY = 0.15

# Profit is normalized on a LOG scale, not a linear one.
#
# Maryland spreads in this catalog range from roughly $40k to $1.2M. A
# linear scale with any fixed ceiling either saturates (every Bethesda and
# Annapolis lead pins at 100 and stops being rankable against each other)
# or crushes ordinary leads to near zero. Log scaling keeps the whole range
# discriminating while still expressing genuine diminishing returns: the
# jump from $50k to $150k of spread matters far more to a wholesaler than
# the jump from $900k to $1M, partly because very expensive houses have a
# much smaller cash-buyer pool and are harder to assign.
PROFIT_NORMALIZATION_CEILING = 1_000_000.0
PROFIT_LOG_SCALE = 10_000.0

# Below this equity percentage there is usually not enough room between
# what is owed and what the house is worth to construct an offer that both
# clears the liens and leaves a margin.
MINIMUM_VIABLE_EQUITY_PERCENT = 10.0


def _flags(prop: dict[str, Any]) -> set[str]:
    quick_lists = prop.get("quick_lists") or []
    return set(quick_lists)


def estimated_profit(prop: dict[str, Any]) -> float | None:
    """Equity available if the house were bought for exactly the payoff.

    Deliberately ignores repair cost, which is not knowable from a
    property/search result (it needs the permit history a per-address
    lookup returns). Same caveat as
    batchdata_client.payoff_profit_estimate(): a prioritization signal,
    not a number to offer.
    """
    value = prop.get("estimated_value")
    liens = prop.get("total_open_lien_balance")
    if value is None:
        return None
    # A free-and-clear property legitimately has no liens; treat missing
    # lien data as zero owed ONLY when the flags corroborate it, otherwise
    # we would invent profit out of missing data.
    if liens is None:
        if "freeAndClear" in _flags(prop):
            liens = 0
        else:
            return None
    # Involuntary liens (tax liens, judgments, mechanic's liens) sit in a
    # separate BatchData field and are NOT included in totalOpenLienBalance,
    # which covers voluntary mortgage debt. They still have to be cleared at
    # closing, so leaving them out overstates the spread on exactly the
    # distressed properties this tool targets.
    involuntary = prop.get("involuntary_lien_total") or 0
    return float(value) - float(liens) - float(involuntary)


def profit_score(prop: dict[str, Any]) -> float:
    profit = estimated_profit(prop)
    if profit is None or profit <= 0:
        return 0.0
    scaled = math.log1p(profit / PROFIT_LOG_SCALE)
    ceiling = math.log1p(PROFIT_NORMALIZATION_CEILING / PROFIT_LOG_SCALE)
    return min(100.0, (scaled / ceiling) * 100.0)


def motivation_score(prop: dict[str, Any]) -> float:
    """Blends foreclosure stage with independent distress signals.

    Stage and signals are combined as a weighted blend rather than summed,
    because a naive sum saturates at the 100 cap almost immediately -- a
    notice-of-sale (90) plus a single failed listing (35) would already
    pin the score, making every late-stage lead look identical and
    destroying the ranking this module exists to produce.

    Stage carries the larger weight because a hard legal deadline drives
    behavior more reliably than an accumulation of soft signals.
    """
    flags = _flags(prop)

    stage = max(
        (score for flag, score in FORECLOSURE_STAGE_SCORES.items() if flag in flags),
        default=0,
    )
    signals = min(
        100,
        sum(score for flag, score in MOTIVATION_SIGNAL_SCORES.items() if flag in flags),
    )
    return float(min(100.0, 0.70 * stage + 0.30 * signals))


def contactability_score(prop: dict[str, Any]) -> float:
    """How likely we are to reach the actual decision-maker.

    An owner living at the property is the easiest case: the mailing
    address is the property address, and skip tracing a person at a known
    residence is far more reliable than chasing an out-of-state owner
    through stale forwarding addresses.
    """
    flags = _flags(prop)
    score = 50.0

    # Prefer the dedicated column over the quickLists flag: the boolean is
    # populated directly from owner.ownerOccupied, whereas the flag is only
    # present when BatchData chose to emit it, so flag-only logic silently
    # under-credits owner-occupied leads.
    owner_occupied = prop.get("owner_occupied")
    if owner_occupied is None:
        owner_occupied = (
            "ownerOccupied" in flags or "samePropertyAndMailingAddress" in flags
        )

    if owner_occupied:
        score += 30.0
    if "absenteeOwnerInState" in flags:
        score += 10.0
    if "absenteeOwnerOutOfState" in flags or "outOfStateOwner" in flags:
        score -= 20.0
    if "mailingAddressVacant" in flags:
        score -= 25.0
    if prop.get("owner_name"):
        score += 10.0

    return max(0.0, min(100.0, score))


def disqualify(prop: dict[str, Any]) -> str | None:
    """Returns a human-readable reason this lead is not worth a call."""
    flags = _flags(prop)
    for flag, reason in DISQUALIFYING_FLAGS.items():
        if flag in flags:
            return reason

    profit = estimated_profit(prop)
    if profit is not None and profit <= 0:
        return "no equity spread (owed meets or exceeds estimated value)"

    equity_percent = prop.get("equity_percent")
    if (
        equity_percent is not None
        and float(equity_percent) < MINIMUM_VIABLE_EQUITY_PERCENT
        and "freeAndClear" not in flags
    ):
        return f"equity below {MINIMUM_VIABLE_EQUITY_PERCENT:.0f}% (no room to construct an offer)"

    return None


def score_property(prop: dict[str, Any]) -> dict[str, Any]:
    """Full score for one property row from the `properties` table.

    The blended score is scaled by a motivation factor rather than being a
    flat weighted sum, so that a completely unmotivated owner cannot ride a
    large spread to the top of the call list. This is the central design
    decision of this module -- see the module docstring.
    """
    profit = estimated_profit(prop)
    p_score = profit_score(prop)
    m_score = motivation_score(prop)
    c_score = contactability_score(prop)

    reason = disqualify(prop)

    blended = (
        WEIGHT_MOTIVATION * m_score
        + WEIGHT_PROFIT * p_score
        + WEIGHT_CONTACTABILITY * c_score
    )
    # Motivation acts as a gate as well as a term: a lead with no urgency
    # signal at all is worth far less than the linear blend suggests.
    motivation_factor = 0.5 + 0.5 * (m_score / 100.0)
    total = 0.0 if reason else round(blended * motivation_factor, 2)

    return {
        "estimated_profit": profit,
        "profit_score": round(p_score, 2),
        "motivation_score": round(m_score, 2),
        "contactability_score": round(c_score, 2),
        "total_score": total,
        "disqualified": reason is not None,
        "disqualified_reason": reason,
        "breakdown": {
            "flags": sorted(_flags(prop)),
            "weights": {
                "motivation": WEIGHT_MOTIVATION,
                "profit": WEIGHT_PROFIT,
                "contactability": WEIGHT_CONTACTABILITY,
            },
            "motivation_factor": round(motivation_factor, 3),
            "blended_before_factor": round(blended, 2),
        },
    }


def score_properties(properties: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Scores many properties, best first. Returns (batchdata_id, score)."""
    scored = [(prop["batchdata_id"], score_property(prop)) for prop in properties]
    scored.sort(key=lambda item: item[1]["total_score"], reverse=True)
    return scored
