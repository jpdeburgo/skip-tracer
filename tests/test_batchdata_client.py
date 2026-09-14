from skip_tracer.batchdata_client import flag_low_margin, max_allowable_offer


def test_max_allowable_offer():
    assert max_allowable_offer(arv=300_000, repair_cost=40_000) == 170_000.0


def test_flag_low_margin_true_when_assessed_meets_arv():
    assert flag_low_margin(assessed_value=310_000, arv_estimate=300_000) is True


def test_flag_low_margin_false_when_room_to_flip():
    assert flag_low_margin(assessed_value=200_000, arv_estimate=300_000) is False
