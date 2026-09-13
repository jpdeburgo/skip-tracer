from skip_tracer.batchdata_client import (
    estimate_arv_from_comps,
    flag_low_margin,
    max_allowable_offer,
)


def test_max_allowable_offer():
    assert max_allowable_offer(arv=300_000, repair_cost=40_000) == 170_000.0


def test_flag_low_margin_true_when_assessed_meets_arv():
    assert flag_low_margin(assessed_value=310_000, arv_estimate=300_000) is True


def test_flag_low_margin_false_when_room_to_flip():
    assert flag_low_margin(assessed_value=200_000, arv_estimate=300_000) is False


def test_estimate_arv_from_comps_weights_recent_sales_higher():
    from datetime import datetime, timedelta

    recent = (datetime.now() - timedelta(days=10)).date().isoformat()
    old = (datetime.now() - timedelta(days=800)).date().isoformat()
    comps = [
        {"price": 200_000, "sqft": 1000, "sale_date": recent},  # $200/sqft
        {"price": 100_000, "sqft": 1000, "sale_date": old},  # $100/sqft, floor weight
    ]
    arv = estimate_arv_from_comps(comps, subject_sqft=1200)
    # Recent comp should dominate the weighted average, pulling ARV/sqft
    # well above the simple midpoint of $150/sqft.
    assert arv / 1200 > 150
