"""Tests for the lead scoring model.

These tests pin down the *behavioral contracts* of the model -- the
relationships that must hold for the ranking to be meaningful -- rather
than the exact numeric weights, which are an explicitly tunable
hypothesis (see the module docstring of skip_tracer.lead_scoring).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def with_foreclosure(prop, *, filed_days_ago=None, auction_in_days=None, status=None):
    """Attach a realistic BatchData `foreclosure` sub-object to a fixture."""
    foreclosure = {}
    if status:
        foreclosure["status"] = status
    if filed_days_ago is not None:
        foreclosure["filingDate"] = (NOW - timedelta(days=filed_days_ago)).isoformat()
    if auction_in_days is not None:
        foreclosure["auctionDate"] = (NOW + timedelta(days=auction_in_days)).isoformat()
    prop["raw"] = {"foreclosure": foreclosure}
    return prop

from skip_tracer.lead_scoring import (
    contactability_score,
    days_to_auction,
    deal_is_constructible,
    disqualify,
    estimated_payoff,
    estimated_profit,
    filing_age_days,
    max_allowable_offer,
    motivation_score,
    profit_score,
    recency_score,
    score_properties,
    score_property,
    timing_factor,
)


def make_property(**overrides):
    """A baseline preforeclosure property; override fields per test."""
    base = {
        "batchdata_id": "prop-1",
        "street": "1 Main St",
        "city": "Baltimore",
        "state": "MD",
        "zip": "21201",
        "owner_name": "Jane Doe",
        "estimated_value": 400_000.0,
        "total_open_lien_balance": 200_000.0,
        "involuntary_lien_total": None,
        "equity_percent": 50.0,
        "quick_lists": ["preforeclosure"],
        "owner_occupied": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Profit
# ---------------------------------------------------------------------------


def test_estimated_profit_subtracts_all_liens():
    """Involuntary liens and stage arrears both come out of the spread."""
    prop = make_property(
        estimated_value=500_000.0,
        total_open_lien_balance=300_000.0,
        involuntary_lien_total=50_000.0,
        quick_lists=["preforeclosure"],
    )
    # 300k principal + 2% preforeclosure arrears + 50k involuntary = 356k
    assert estimated_profit(prop) == pytest.approx(144_000.0)


def test_estimated_payoff_grows_with_foreclosure_stage():
    """Arrears, trustee and legal fees compound as the case advances, so
    the raw loan balance understates payoff most on late-stage leads."""
    early = make_property(quick_lists=["preforeclosure", "noticeOfDefault"])
    late = make_property(quick_lists=["preforeclosure", "noticeOfSale"])
    assert estimated_payoff(late) > estimated_payoff(early)


def test_estimated_payoff_is_none_without_lien_data():
    assert estimated_payoff(make_property(total_open_lien_balance=None)) is None


def test_free_and_clear_payoff_is_zero_without_lien_data():
    prop = make_property(total_open_lien_balance=None, quick_lists=["freeAndClear"])
    assert estimated_payoff(prop) == 0.0


def test_estimated_profit_is_none_without_a_valuation():
    assert estimated_profit(make_property(estimated_value=None)) is None


def test_profit_score_is_zero_when_underwater():
    prop = make_property(estimated_value=200_000.0, total_open_lien_balance=300_000.0)
    assert profit_score(prop) == 0.0


def test_profit_score_is_zero_when_profit_is_unknown():
    assert profit_score(make_property(estimated_value=None)) == 0.0


def test_profit_score_is_monotonic_in_profit():
    small = make_property(estimated_value=250_000.0, total_open_lien_balance=200_000.0)
    medium = make_property(estimated_value=450_000.0, total_open_lien_balance=200_000.0)
    large = make_property(estimated_value=900_000.0, total_open_lien_balance=200_000.0)
    assert profit_score(small) < profit_score(medium) < profit_score(large)


def test_profit_score_does_not_saturate_across_the_maryland_range():
    """The log scale exists so expensive leads stay rankable against each other.

    A linear scale made every high-equity lead pin at 100, which produced
    nine-way ties and destroyed the ordering this model exists to create.
    """
    mid = make_property(estimated_value=700_000.0, total_open_lien_balance=200_000.0)
    high = make_property(estimated_value=1_400_000.0, total_open_lien_balance=200_000.0)
    assert profit_score(mid) < profit_score(high)
    assert profit_score(mid) < 100.0


def test_profit_score_is_capped_at_100():
    absurd = make_property(estimated_value=50_000_000.0, total_open_lien_balance=0.0)
    assert profit_score(absurd) == 100.0


# ---------------------------------------------------------------------------
# Motivation
# ---------------------------------------------------------------------------


def test_early_foreclosure_stage_outranks_late():
    """Deliberately inverted from the naive "closer to auction = better".

    Maryland gives only 10-30 days' notice of sale, which is not enough
    runway to skip-trace, reach an owner, negotiate and close an
    assignment. Arrears also compound into the payoff as the case advances,
    so late stages are where the equity has already been eaten. Lis pendens
    is the real entry point in a judicial state.
    """
    lis_pendens = make_property(quick_lists=["preforeclosure", "noticeOfLisPendens"])
    sale = make_property(quick_lists=["preforeclosure", "noticeOfSale"])
    auction = make_property(quick_lists=["preforeclosure", "activeAuction"])
    assert motivation_score(lis_pendens) > motivation_score(sale)
    assert motivation_score(sale) > motivation_score(auction)


def test_tax_default_outranks_late_stage_foreclosure():
    """Tax debt is small relative to value while mortgage debt is large, so
    a tax-delinquent owner still has a constructible spread. It is a
    motivation signal that does not also destroy the margin."""
    tax = make_property(quick_lists=["preforeclosure", "taxDefault"])
    sale = make_property(quick_lists=["preforeclosure", "noticeOfSale"])
    assert motivation_score(tax) > motivation_score(sale)


def test_expired_listing_outranks_failed_listing():
    """Verified on the real catalog: expiredListing is a strict subset of
    failedListing. A failed listing was withdrawn BEFORE the contract
    expired, so it may still carry broker-commission exposure and the owner
    may have decided not to sell at all."""
    expired = make_property(quick_lists=["preforeclosure", "expiredListing"])
    failed = make_property(quick_lists=["preforeclosure", "failedListing"])
    assert motivation_score(expired) > motivation_score(failed)


def test_absentee_owner_alone_adds_no_motivation():
    """A content landlord is not a motivated seller; bare absentee
    ownership is a low-conversion signal that only counts when stacked."""
    plain = make_property(quick_lists=["preforeclosure"])
    absentee = make_property(quick_lists=["preforeclosure", "absenteeOwner"])
    assert motivation_score(absentee) == motivation_score(plain)


def test_additional_distress_signals_raise_motivation():
    plain = make_property(quick_lists=["preforeclosure", "noticeOfSale"])
    compounded = make_property(
        quick_lists=["preforeclosure", "noticeOfSale", "taxDefault", "vacant"]
    )
    assert motivation_score(compounded) > motivation_score(plain)


def test_motivation_does_not_saturate_on_two_signals():
    """Guards the regression that flattened the whole top of the list.

    Summing stage and signals meant notice-of-sale plus one failed listing
    already hit the cap, so genuinely different leads scored identically.
    """
    two_signals = make_property(
        quick_lists=["preforeclosure", "noticeOfSale", "failedListing"]
    )
    assert motivation_score(two_signals) < 100.0


def test_motivation_never_exceeds_100():
    everything = make_property(
        quick_lists=[
            "preforeclosure",
            "activeAuction",
            "taxDefault",
            "vacant",
            "absenteeOwner",
            "failedListing",
            "expiredListing",
            "noticeOfLisPendens",
        ]
    )
    assert motivation_score(everything) <= 100.0


def test_property_with_no_distress_flags_has_zero_motivation():
    assert motivation_score(make_property(quick_lists=[])) == 0.0


# ---------------------------------------------------------------------------
# Contactability
# ---------------------------------------------------------------------------


def test_owner_occupied_is_more_contactable_than_absentee():
    occupied = make_property(owner_occupied=True)
    absentee = make_property(owner_occupied=False)
    assert contactability_score(occupied) > contactability_score(absentee)


def test_missing_owner_name_lowers_contactability():
    named = make_property(owner_name="Jane Doe")
    anonymous = make_property(owner_name=None)
    assert contactability_score(anonymous) < contactability_score(named)


# ---------------------------------------------------------------------------
# Disqualification
# ---------------------------------------------------------------------------


def test_active_listing_is_disqualifying():
    prop = make_property(quick_lists=["preforeclosure", "activeListing"])
    assert disqualify(prop) is not None


def test_failed_listing_is_not_disqualifying():
    """A failed listing is the opposite of an active one: proven intent to
    sell with no broker contract in the way."""
    prop = make_property(quick_lists=["preforeclosure", "failedListing"])
    assert disqualify(prop) is None


def test_underwater_property_is_disqualified():
    prop = make_property(estimated_value=200_000.0, total_open_lien_balance=260_000.0)
    assert disqualify(prop) is not None


def test_ordinary_preforeclosure_is_not_disqualified():
    assert disqualify(make_property()) is None


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------


def test_score_property_reports_every_component():
    score = score_property(make_property())
    for key in (
        "total_score",
        "profit_score",
        "motivation_score",
        "contactability_score",
        "estimated_profit",
        "disqualified",
    ):
        assert key in score


def test_disqualified_property_scores_zero():
    prop = make_property(quick_lists=["preforeclosure", "activeListing"])
    score = score_property(prop)
    assert score["disqualified"]
    assert score["total_score"] == 0.0


def test_motivation_outranks_raw_profit():
    """The central design claim of the model.

    Profit is what a deal is worth *if* it closes; motivation drives
    whether it closes at all. A huge-equity owner with no urgency must not
    outrank an urgent owner with a modest spread, or the call list sends
    the user to people who will not sell.
    """
    rich_but_calm = make_property(
        batchdata_id="calm",
        estimated_value=1_500_000.0,
        total_open_lien_balance=200_000.0,
        quick_lists=["preforeclosure"],
    )
    urgent_but_modest = make_property(
        batchdata_id="urgent",
        estimated_value=400_000.0,
        total_open_lien_balance=120_000.0,
        quick_lists=["preforeclosure", "noticeOfDefault", "vacant", "taxDefault"],
    )
    assert (
        score_property(urgent_but_modest)["total_score"]
        > score_property(rich_but_calm)["total_score"]
    )


def test_score_properties_sorts_descending_and_keys_by_id():
    props = [
        make_property(batchdata_id="low", quick_lists=["preforeclosure"]),
        make_property(
            batchdata_id="high",
            quick_lists=["preforeclosure", "noticeOfSale", "vacant"],
        ),
    ]
    ranked = score_properties(props)
    assert [batchdata_id for batchdata_id, _ in ranked] == ["high", "low"]


def test_score_properties_pushes_disqualified_to_the_bottom():
    props = [
        make_property(batchdata_id="listed", quick_lists=["preforeclosure", "activeListing"]),
        make_property(batchdata_id="callable", quick_lists=["preforeclosure"]),
    ]
    ranked = score_properties(props)
    assert ranked[-1][0] == "listed"


def test_scoring_handles_a_property_with_no_financial_data():
    """Real BatchData rows arrive with missing fields; the scorer must not
    crash mid-run and lose the rest of the batch."""
    sparse = {
        "batchdata_id": "sparse",
        "street": None,
        "city": None,
        "state": "MD",
        "zip": None,
        "owner_name": None,
        "estimated_value": None,
        "total_open_lien_balance": None,
        "involuntary_lien_total": None,
        "equity_percent": None,
        "quick_lists": [],
        "owner_occupied": None,
    }
    score = score_property(sparse)
    assert score["total_score"] >= 0.0


# ---------------------------------------------------------------------------
# Recency and timing
# ---------------------------------------------------------------------------


def test_filing_age_is_computed_from_the_filing_date():
    prop = with_foreclosure(make_property(), filed_days_ago=45)
    assert filing_age_days(prop, NOW) == 45


def test_filing_age_is_none_without_a_date():
    assert filing_age_days(make_property(), NOW) is None


def test_fresh_filings_score_higher_than_stale_ones():
    fresh = with_foreclosure(make_property(), filed_days_ago=20)
    mid = with_foreclosure(make_property(), filed_days_ago=120)
    stale = with_foreclosure(make_property(), filed_days_ago=400)
    assert recency_score(fresh, NOW) > recency_score(mid, NOW) > recency_score(stale, NOW)


def test_unknown_filing_date_is_neither_rewarded_nor_punished():
    assert recency_score(make_property(), NOW) == 50.0


def test_days_to_auction_handles_future_and_past():
    future = with_foreclosure(make_property(), filed_days_ago=30, auction_in_days=45)
    past = with_foreclosure(make_property(), filed_days_ago=300, auction_in_days=-90)
    assert days_to_auction(future, NOW) == 45
    assert days_to_auction(past, NOW) == -90


def test_auction_date_before_filing_date_is_discarded():
    """A real catalog entry was filed in 2026 with a 2014 auctionDate --
    a stale record from an older foreclosure. Trusting it would score a
    dead lead as urgent."""
    prop = with_foreclosure(make_property(), filed_days_ago=30, auction_in_days=-4000)
    assert days_to_auction(prop, NOW) is None


def test_timing_factor_penalizes_a_passed_auction():
    passed = with_foreclosure(make_property(), filed_days_ago=300, auction_in_days=-60)
    assert timing_factor(passed, NOW) < 1.0


def test_timing_factor_penalizes_an_imminent_auction():
    """Not enough runway to skip-trace, negotiate and close an assignment."""
    imminent = with_foreclosure(make_property(), filed_days_ago=20, auction_in_days=5)
    assert timing_factor(imminent, NOW) < 1.0


def test_timing_factor_is_neutral_with_adequate_runway():
    workable = with_foreclosure(make_property(), filed_days_ago=20, auction_in_days=60)
    assert timing_factor(workable, NOW) == 1.0


def test_timing_factor_is_neutral_without_an_auction_date():
    assert timing_factor(make_property(), NOW) == 1.0


def test_live_lead_outranks_an_identical_one_whose_auction_passed():
    """The single biggest real-world discriminator: 39 of 48 Maryland
    properties have an auction date already in the past."""
    live = with_foreclosure(
        make_property(batchdata_id="live"), filed_days_ago=25, auction_in_days=60
    )
    dead = with_foreclosure(
        make_property(batchdata_id="dead"), filed_days_ago=300, auction_in_days=-60
    )
    assert score_property(live, NOW)["total_score"] > score_property(dead, NOW)["total_score"]


# ---------------------------------------------------------------------------
# The 70% rule
# ---------------------------------------------------------------------------


def test_max_allowable_offer_follows_the_seventy_percent_rule():
    mao = max_allowable_offer(make_property(estimated_value=400_000.0))
    # 0.70 * (400k * 1.15) - 60k repairs - 16k fee
    assert mao == pytest.approx(322_000.0 - 60_000.0 - 16_000.0)


def test_max_allowable_offer_is_none_without_a_valuation():
    assert max_allowable_offer(make_property(estimated_value=None)) is None


def test_thin_equity_fails_the_seventy_percent_rule():
    thin = make_property(estimated_value=400_000.0, total_open_lien_balance=300_000.0)
    assert deal_is_constructible(thin) is False
    assert "70% rule" in disqualify(thin)


def test_healthy_equity_passes_the_seventy_percent_rule():
    healthy = make_property(estimated_value=400_000.0, total_open_lien_balance=120_000.0)
    assert deal_is_constructible(healthy) is True
    assert disqualify(healthy) is None


def test_equity_percent_is_only_a_fallback():
    """When lien data exists we run the payoff test directly; the
    percentage is a proxy used only when we cannot."""
    no_liens = make_property(
        estimated_value=400_000.0, total_open_lien_balance=None, equity_percent=2.0
    )
    assert deal_is_constructible(no_liens) is None
    assert "equity below" in disqualify(no_liens)


def test_score_exposes_payoff_and_mao_for_auditing():
    score = score_property(make_property(), NOW)
    assert score["estimated_payoff"] is not None
    assert score["max_allowable_offer"] is not None
    assert "days_to_auction" in score["breakdown"]
    assert "timing_factor" in score["breakdown"]
