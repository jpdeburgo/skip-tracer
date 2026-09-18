from skip_tracer.batchdata_client import (
    flag_low_margin,
    max_allowable_offer,
    payoff_profit_estimate,
)


def test_max_allowable_offer():
    assert max_allowable_offer(arv=300_000, repair_cost=40_000) == 170_000.0


def test_flag_low_margin_true_when_assessed_meets_arv():
    assert flag_low_margin(assessed_value=310_000, arv_estimate=300_000) is True


def test_flag_low_margin_false_when_room_to_flip():
    assert flag_low_margin(assessed_value=200_000, arv_estimate=300_000) is False


def test_payoff_profit_estimate_positive_when_arv_covers_debt_and_repairs():
    profit = payoff_profit_estimate(
        arv_estimate=300_000, total_lien_balance=150_000, repair_cost=40_000
    )
    assert profit == 110_000.0


def test_payoff_profit_estimate_negative_when_debt_exceeds_arv():
    profit = payoff_profit_estimate(
        arv_estimate=300_000, total_lien_balance=280_000, repair_cost=40_000
    )
    assert profit == -20_000.0
