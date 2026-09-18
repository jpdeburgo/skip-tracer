"""Tests for the lead scoring model.

These tests pin down the *behavioral contracts* of the model -- the
relationships that must hold for the ranking to be meaningful -- rather
than the exact numeric weights, which are an explicitly tunable
hypothesis (see the module docstring of skip_tracer.lead_scoring).
"""

from __future__ import annotations

import pytest

from skip_tracer.lead_scoring import (
    contactability_score,
    disqualify,
    estimated_profit,
    motivation_score,
    profit_score,
    score_properties,
    score_property,
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
    prop = make_property(
        estimated_value=500_000.0,
        total_open_lien_balance=300_000.0,
        involuntary_lien_total=50_000.0,
    )
    assert estimated_profit(prop) == pytest.approx(150_000.0)


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


def test_later_foreclosure_stage_scores_higher():
    default = make_property(quick_lists=["preforeclosure", "noticeOfDefault"])
    sale = make_property(quick_lists=["preforeclosure", "noticeOfSale"])
    assert motivation_score(default) < motivation_score(sale)


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
        estimated_value=300_000.0,
        total_open_lien_balance=180_000.0,
        quick_lists=["preforeclosure", "noticeOfSale", "vacant", "taxDefault"],
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
