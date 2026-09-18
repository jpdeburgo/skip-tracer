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
from datetime import date, datetime, timezone
from typing import Any

# --------------------------------------------------------------------------
# Foreclosure-stage urgency.
#
# REVISED against external research (see README "Lead scoring") and against
# this repo's own 48-property Maryland catalog. The original ordering put
# `activeAuction` and `noticeOfSale` at the top on the theory that a hard
# deadline is the strongest motivator. Two findings overturned that:
#
#  1. Maryland gives only 10-30 days' notice of sale. That is not enough
#     runway to skip-trace, reach an owner, negotiate, and close an
#     assignment. By notice-of-sale the deal is usually mechanically
#     impossible, not merely urgent.
#  2. Arrears, trustee fees and legal costs compound into the payoff as the
#     case progresses, so the late stages are exactly where the equity we
#     are scoring has already been eaten -- and where our spread estimate
#     is least trustworthy (see PAYOFF_ARREARS_HAIRCUT below).
#
# The practical entry point in Maryland (a judicial state) is the Order to
# Docket / lis pendens: the owner knows it is real, but there are still
# months of runway. That is now the top-scoring stage.
#
# Measured against this repo's own catalog, `preforeclosure` is an umbrella
# flag that every more-specific stage also carries, so it scores lowest on
# its own -- it is the floor, not a signal.
# --------------------------------------------------------------------------
FORECLOSURE_STAGE_SCORES = {
    "noticeOfLisPendens": 100,
    "noticeOfDefault": 95,
    "noticeOfSale": 55,
    "activeAuction": 25,
    "preforeclosure": 35,
}

# --------------------------------------------------------------------------
# Additional distress/motivation signals, scored independently of stage.
#
# `expiredListing` outranks `failedListing` deliberately. BatchData follows
# the industry definition in which a failed listing is one WITHDRAWN BEFORE
# the listing contract expires, while an expired listing ran its full term.
# Verified on this repo's own catalog: the single `expiredListing` property
# also carries `failedListing`, i.e. expired is a strict subset of failed.
# That matters because a withdrawn-but-unexpired listing may still be under
# an exclusive agreement the seller owes commission on, and because an
# owner who pulled their listing may have decided NOT to sell -- whereas an
# expired listing is unambiguously a seller who tried, failed, and is now
# free of the broker. Neither co-occurs with `activeListing`/`onMarket` in
# our data, so neither is currently on the MLS; `failedListing` simply
# carries contract risk that `expiredListing` does not.
#
# `taxDefault` is raised above the mid foreclosure stages on a structural
# argument: tax debt is small relative to value (thousands) while mortgage
# debt is large relative to value (hundreds of thousands). A tax-delinquent
# owner therefore almost by construction still has a constructible spread.
# Tax default is a motivation signal that does not simultaneously destroy
# the margin, which is precisely the failure mode of late-stage
# foreclosure. Caveat: it is a NOISIER signal -- some owners simply forgot
# or are disputing the bill -- so it is strong but not decisive on its own.
#
# `vacant` scores high because an empty house is a pure carrying cost with
# no offsetting benefit to the owner. Note this pipeline uses BatchData's
# paid vacancy flag, not an unreliable free/USPS-derived list. It is also
# rare in Maryland (4 of 72 here; ATTOM counts ~151 zombie foreclosures
# statewide per quarter), so it is a high-precision tripwire rather than a
# load-bearing term.
#
# `absenteeOwner` is deliberately absent. Absentee ownership on its own is
# a low-conversion signal -- a content landlord is not a motivated seller.
# It earns its keep only when stacked with genuine distress, which the
# other terms here already capture.
# --------------------------------------------------------------------------
# `taxDefault` is treated as a parallel distress STAGE rather than an
# additive signal, because it is a track of its own: the owner is at risk
# of losing the house to a tax sale independently of any mortgage. Scoring
# it as a mere signal let the 0.30 signal weight dilute it below a
# notice-of-sale, which inverts the structural argument above.
TAX_DEFAULT_STAGE_SCORE = 75

MOTIVATION_SIGNAL_SCORES = {
    "expiredListing": 40,
    "vacant": 30,
    "failedListing": 20,
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
# clears the liens and leaves a margin. Used only as a FALLBACK when lien
# data is missing -- when we have lien data we run the payoff test below,
# which is the real constraint rather than a proxy for it.
MINIMUM_VIABLE_EQUITY_PERCENT = 10.0

# --------------------------------------------------------------------------
# The 70% rule -- the actual constraint a wholesale deal has to satisfy.
#
#   max investor price = 0.70 * ARV - repairs
#   our max offer      = 0.70 * ARV - repairs - our fee
#
# so a deal is only constructible when total payoff <= that number. This is
# the direct test; equity percent is merely a proxy for it. Solving for the
# required equity percentage gives
#
#   equity% >= 30% + 0.30 * (repairs/value) + (fee/value)
#
# which lands at ~35-41% for light-to-heavy rehab -- independently matching
# the "40%+ equity" threshold practitioners quote. We apply the formula
# rather than a flat percentage so the bar rises with the repair burden.
INVESTOR_MARGIN_RULE = 0.70
ASSIGNMENT_FEE_FRACTION = 0.04

# We cannot see repair cost from a property/search result (that needs the
# permit history a per-address lookup returns), so we assume a typical
# rehab. This is the single largest unmeasured quantity in the model: a
# property needing a gut renovation will look far better here than it is.
ASSUMED_REPAIR_FRACTION = 0.15

# --------------------------------------------------------------------------
# Arrears haircut by foreclosure stage.
#
# `totalOpenLienBalance` is the LOAN BALANCE, not the payoff. Missed
# payments, late fees, trustee costs, legal fees and advanced taxes all
# accrue on top, and they compound the further the case has progressed.
# Ignoring this makes late-stage leads look more profitable than they are,
# which is doubly wrong because those are the same leads whose motivation
# we have already down-weighted. The fractions below are an explicit,
# tunable guess -- BatchData does not expose a payoff figure.
# --------------------------------------------------------------------------
PAYOFF_ARREARS_HAIRCUT = {
    "activeAuction": 0.12,
    "noticeOfSale": 0.10,
    "noticeOfLisPendens": 0.06,
    "noticeOfDefault": 0.04,
    "preforeclosure": 0.02,
}

# --------------------------------------------------------------------------
# Recency / timing.
#
# A distress flag is close to meaningless without a date attached, and
# BatchData ships real ones: `foreclosure.filingDate` is populated on 100%
# of our Maryland catalog and `auctionDate` on 87%. Two independent effects:
#
#  1. FILING RECENCY. A filing from three weeks ago is a live situation; one
#     from ten months ago has usually resolved one way or another. Response
#     rates are reported to be much higher inside a 30-90 day window.
#  2. RUNWAY TO AUCTION. This is a hard feasibility gate, not a preference.
#     Below MIN_DAYS_TO_CLOSE there is not enough time to skip-trace, reach
#     the owner, negotiate and close an assignment, so the lead is
#     unworkable no matter how motivated the owner is.
#
# An auction date that has already passed is heavily penalized: the house
# most likely sold or the case was resolved. Note that 39 of our 48
# Maryland properties are in exactly that state, so this term does most of
# the work of separating live leads from dead ones.
# --------------------------------------------------------------------------
FILING_RECENCY_SCORES = (
    (30, 100),
    (90, 90),
    (180, 60),
    (365, 30),
)
FILING_RECENCY_STALE_SCORE = 10

MIN_DAYS_TO_CLOSE = 21
AUCTION_PASSED_PENALTY = 0.35
AUCTION_IMMINENT_PENALTY = 0.50


def _flags(prop: dict[str, Any]) -> set[str]:
    quick_lists = prop.get("quick_lists") or []
    return set(quick_lists)


def _stage(prop: dict[str, Any]) -> str | None:
    """The most advanced foreclosure stage flagged on this property."""
    flags = _flags(prop)
    stages = [f for f in FORECLOSURE_STAGE_SCORES if f in flags and f != "preforeclosure"]
    if stages:
        return min(stages, key=lambda f: FORECLOSURE_STAGE_SCORES[f])
    return "preforeclosure" if "preforeclosure" in flags else None


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _foreclosure(prop: dict[str, Any]) -> dict[str, Any]:
    raw = prop.get("raw") or {}
    return (raw.get("foreclosure") if isinstance(raw, dict) else None) or {}


def filing_age_days(prop: dict[str, Any], now: datetime | None = None) -> int | None:
    filed = _parse_date(_foreclosure(prop).get("filingDate"))
    if filed is None:
        return None
    return ((now or datetime.now(timezone.utc)) - filed).days


def days_to_auction(prop: dict[str, Any], now: datetime | None = None) -> int | None:
    """Days until the scheduled auction, or negative if it has passed.

    Returns None when the date is absent or incoherent. An auctionDate
    EARLIER than the filingDate is a stale record carried over from an
    older foreclosure event -- our catalog has one property filed in 2026
    whose auctionDate reads 2014. Trusting that would have scored a dead
    lead as urgent, so incoherent pairs are discarded rather than used.
    """
    foreclosure = _foreclosure(prop)
    auction = _parse_date(foreclosure.get("auctionDate"))
    if auction is None:
        return None
    filed = _parse_date(foreclosure.get("filingDate"))
    if filed is not None and auction < filed:
        return None
    return (auction - (now or datetime.now(timezone.utc))).days


def recency_score(prop: dict[str, Any], now: datetime | None = None) -> float:
    """How live this distress signal still is, from the filing date."""
    age = filing_age_days(prop, now)
    if age is None:
        return 50.0  # unknown: neither rewarded nor punished
    if age < 0:
        return 100.0
    for limit, score in FILING_RECENCY_SCORES:
        if age <= limit:
            return float(score)
    return float(FILING_RECENCY_STALE_SCORE)


def timing_factor(prop: dict[str, Any], now: datetime | None = None) -> float:
    """Feasibility multiplier based on runway to the auction.

    Separate from motivation on purpose: an owner facing an auction in five
    days may be maximally motivated and still be unreachable-and-closeable
    in time. This scales the whole lead rather than one axis, because
    running out of clock invalidates the deal, not just the urgency.
    """
    days = days_to_auction(prop, now)
    if days is None:
        return 1.0
    if days < 0:
        return AUCTION_PASSED_PENALTY
    if days < MIN_DAYS_TO_CLOSE:
        return AUCTION_IMMINENT_PENALTY
    return 1.0


def estimated_payoff(prop: dict[str, Any]) -> float | None:
    """Total cash needed to clear title, not merely the loan balance.

    `totalOpenLienBalance` is the principal owed. Arrears, late fees,
    trustee and legal costs accrue on top and grow with the stage of the
    case, so the raw balance understates the real payoff -- and understates
    it most on exactly the late-stage leads where the margin is thinnest.
    """
    liens = prop.get("total_open_lien_balance")
    if liens is None:
        if "freeAndClear" in _flags(prop):
            liens = 0
        else:
            return None
    involuntary = prop.get("involuntary_lien_total") or 0
    haircut = PAYOFF_ARREARS_HAIRCUT.get(_stage(prop) or "", 0.0)
    return float(liens) * (1.0 + haircut) + float(involuntary)


def estimated_profit(prop: dict[str, Any]) -> float | None:
    """Equity available if the house were bought for exactly the payoff.

    Deliberately ignores repair cost, which is not knowable from a
    property/search result (it needs the permit history a per-address
    lookup returns). Same caveat as
    batchdata_client.payoff_profit_estimate(): a prioritization signal,
    not a number to offer.
    """
    value = prop.get("estimated_value")
    payoff = estimated_payoff(prop)
    if value is None or payoff is None:
        return None
    return float(value) - payoff


def max_allowable_offer(prop: dict[str, Any]) -> float | None:
    """The 70% rule, net of assumed repairs and our assignment fee.

    NOTE: the rule takes ARV (after-repair value) but BatchData's
    estimatedValue is an as-is AVM. We approximate ARV as value + repairs,
    which is the best available from a search result and is the single
    biggest modelling assumption here.
    """
    value = prop.get("estimated_value")
    if value is None:
        return None
    value = float(value)
    repairs = value * ASSUMED_REPAIR_FRACTION
    arv = value + repairs
    fee = value * ASSIGNMENT_FEE_FRACTION
    return INVESTOR_MARGIN_RULE * arv - repairs - fee


def deal_is_constructible(prop: dict[str, Any]) -> bool | None:
    """Can we clear the payoff and still leave the buyer their margin?

    This is the real constraint. Equity percent is only a proxy for it, so
    when we have lien data we test it directly and fall back to the
    percentage only when we do not.
    """
    payoff = estimated_payoff(prop)
    mao = max_allowable_offer(prop)
    if payoff is None or mao is None:
        return None
    return payoff <= mao


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
    if "taxDefault" in flags:
        stage = max(stage, TAX_DEFAULT_STAGE_SCORE)
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
        return "no equity spread (owed meets or exceeds estimated payoff)"

    # The direct 70%-rule test, preferred over the equity-percent proxy
    # whenever we have the lien data to run it.
    constructible = deal_is_constructible(prop)
    if constructible is False:
        return "fails the 70% rule (payoff exceeds max allowable offer)"

    if constructible is None:
        equity_percent = prop.get("equity_percent")
        if (
            equity_percent is not None
            and float(equity_percent) < MINIMUM_VIABLE_EQUITY_PERCENT
            and "freeAndClear" not in flags
        ):
            return (
                f"equity below {MINIMUM_VIABLE_EQUITY_PERCENT:.0f}% "
                "(no room to construct an offer)"
            )

    return None


def score_property(prop: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Full score for one property row from the `properties` table.

    The blended score is scaled by a motivation factor rather than being a
    flat weighted sum, so that a completely unmotivated owner cannot ride a
    large spread to the top of the call list. This is the central design
    decision of this module -- see the module docstring.

    It is then scaled again by a timing factor, which is a separate idea:
    motivation is whether the owner WANTS to act, timing is whether there
    is still enough clock left for a deal to physically close.
    """
    profit = estimated_profit(prop)
    p_score = profit_score(prop)
    m_score = motivation_score(prop)
    c_score = contactability_score(prop)
    r_score = recency_score(prop, now)

    reason = disqualify(prop)

    blended = (
        WEIGHT_MOTIVATION * m_score
        + WEIGHT_PROFIT * p_score
        + WEIGHT_CONTACTABILITY * c_score
    )
    # Motivation acts as a gate as well as a term: a lead with no urgency
    # signal at all is worth far less than the linear blend suggests.
    motivation_factor = 0.5 + 0.5 * (m_score / 100.0)
    # Recency scales the same way: a filing from ten months ago describes a
    # situation that has very likely already resolved.
    recency_factor = 0.5 + 0.5 * (r_score / 100.0)
    timing = timing_factor(prop, now)

    total = (
        0.0
        if reason
        else round(blended * motivation_factor * recency_factor * timing, 2)
    )

    return {
        "estimated_profit": profit,
        "estimated_payoff": estimated_payoff(prop),
        "max_allowable_offer": max_allowable_offer(prop),
        "profit_score": round(p_score, 2),
        "motivation_score": round(m_score, 2),
        "contactability_score": round(c_score, 2),
        "recency_score": round(r_score, 2),
        "total_score": total,
        "disqualified": reason is not None,
        "disqualified_reason": reason,
        "breakdown": {
            "flags": sorted(_flags(prop)),
            "stage": _stage(prop),
            "weights": {
                "motivation": WEIGHT_MOTIVATION,
                "profit": WEIGHT_PROFIT,
                "contactability": WEIGHT_CONTACTABILITY,
            },
            "motivation_factor": round(motivation_factor, 3),
            "recency_factor": round(recency_factor, 3),
            "timing_factor": round(timing, 3),
            "filing_age_days": filing_age_days(prop, now),
            "days_to_auction": days_to_auction(prop, now),
            "blended_before_factor": round(blended, 2),
        },
    }


def score_properties(
    properties: list[dict[str, Any]], now: datetime | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """Scores many properties, best first. Returns (batchdata_id, score)."""
    scored = [(prop["batchdata_id"], score_property(prop, now)) for prop in properties]
    scored.sort(key=lambda item: item[1]["total_score"], reverse=True)
    return scored
