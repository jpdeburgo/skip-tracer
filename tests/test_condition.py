from skip_tracer.condition import condition_tier, estimate_repair_cost


def test_likely_updated_with_high_grade_and_recent_permit():
    record = {"STRUGRAD": "8"}
    permits = [{"year": 2022}]
    assert condition_tier(record, permits) == "likely-updated"


def test_likely_dated_with_low_grade_and_no_recent_permits():
    record = {"STRUGRAD": "3"}
    permits = [{"year": 2005}]
    assert condition_tier(record, permits) == "likely-dated"


def test_unassessed_with_no_grade():
    assert condition_tier({}, []) == "unassessed"


def test_unassessed_when_high_grade_but_no_recent_permits():
    record = {"STRUGRAD": "8"}
    assert condition_tier(record, []) == "unassessed"


def test_unassessed_when_low_grade_but_has_recent_permits():
    record = {"STRUGRAD": "3"}
    permits = [{"year": 2023}]
    assert condition_tier(record, permits) == "unassessed"


def test_unassessed_for_mid_range_grade():
    record = {"STRUGRAD": "5"}
    assert condition_tier(record, []) == "unassessed"


def test_estimate_repair_cost_for_likely_updated():
    assert estimate_repair_cost("likely-updated", 1000) == 12_000.0


def test_estimate_repair_cost_for_likely_dated():
    assert estimate_repair_cost("likely-dated", 1000) == 40_000.0


def test_estimate_repair_cost_none_for_unassessed_tier():
    assert estimate_repair_cost("unassessed", 1000) is None


def test_estimate_repair_cost_none_without_square_footage():
    assert estimate_repair_cost("likely-dated", None) is None
    assert estimate_repair_cost("likely-dated", 0) is None
