from skip_tracer.batchdata_client import (
    BatchDataClient,
    flag_low_margin,
    max_allowable_offer,
    payoff_profit_estimate,
    rank_properties_by_profit,
)


class _FakeSession:
    """Records the JSON body posted, so search_properties() can be
    verified without a real network call or API key."""

    def __init__(self, response_json):
        self.response_json = response_json
        self.last_json_body = None

    def post(self, url, headers, json, timeout):  # noqa: A002 - matches requests' kwarg name
        self.last_json_body = json
        return self


def test_search_properties_posts_search_criteria_and_pagination_options():
    fake_session = _FakeSession({"results": {"properties": []}})
    fake_session.raise_for_status = lambda: None
    fake_session.json = lambda: fake_session.response_json
    client = BatchDataClient("test-token", session=fake_session)

    result = client.search_properties(
        {"query": "Montgomery County, MD", "quickLists": ["preforeclosure"]},
        skip=0,
        take=10,
    )

    assert result == {"results": {"properties": []}}
    assert fake_session.last_json_body == {
        "searchCriteria": {
            "query": "Montgomery County, MD",
            "quickLists": ["preforeclosure"],
        },
        "skip": 0,
        "take": 10,
    }


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


def test_rank_properties_by_profit_sorts_descending_and_annotates():
    properties = [
        {
            "_id": "low-profit",
            "valuation": {"estimatedValue": 300_000},
            "openLien": {"totalOpenLienBalance": 280_000},
        },
        {
            "_id": "high-profit",
            "valuation": {"estimatedValue": 500_000},
            "openLien": {"totalOpenLienBalance": 100_000},
        },
        {
            "_id": "missing-data",
            "valuation": {},
            "openLien": {},
        },
    ]

    ranked = rank_properties_by_profit(properties)

    assert [p["_id"] for p in ranked] == ["high-profit", "low-profit", "missing-data"]
    assert ranked[0]["_estimated_profit"] == 400_000.0
    assert ranked[1]["_estimated_profit"] == 20_000.0
    assert ranked[2]["_estimated_profit"] is None


def test_rank_properties_by_profit_respects_top_n():
    properties = [
        {"_id": "a", "valuation": {"estimatedValue": 200_000}, "openLien": {"totalOpenLienBalance": 100_000}},
        {"_id": "b", "valuation": {"estimatedValue": 400_000}, "openLien": {"totalOpenLienBalance": 100_000}},
        {"_id": "c", "valuation": {"estimatedValue": 300_000}, "openLien": {"totalOpenLienBalance": 100_000}},
    ]

    ranked = rank_properties_by_profit(properties, top_n=2)

    assert [p["_id"] for p in ranked] == ["b", "c"]
