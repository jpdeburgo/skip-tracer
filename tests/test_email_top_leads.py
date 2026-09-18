"""Tests for the top-leads email digest.

The digest is the product's main output surface, so these focus on the
translation layer between database rows and the call-prep logic: getting a
flag mapping wrong silently produces a generic opener for an urgent lead,
which is exactly the failure the transcript work existed to prevent.
"""

from __future__ import annotations

import email_top_leads as digest


def make_row(**overrides):
    row = {
        "id": 1,
        "batchdata_id": "abc123",
        "street": "1 Main St",
        "city": "Bethesda",
        "county": "Montgomery",
        "zip": "20814",
        "owner_name": "Jane Doe",
        "owner_occupied": True,
        "estimated_value": 800_000,
        "total_open_lien_balance": 300_000,
        "equity_percent": 62.5,
        "estimated_profit": 500_000,
        "profit_score": 88.0,
        "motivation_score": 80.0,
        "contactability_score": 90.0,
        "total_score": 84.0,
        "status": "new",
        "phone": None,
        "do_not_call": None,
        "quick_lists": ["preforeclosure", "noticeOfSale", "highEquity"],
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Flag translation
# ---------------------------------------------------------------------------


def test_flags_are_ordered_most_urgent_first():
    """call_script() takes the FIRST matching signal, so a notice of sale
    must outrank the generic preforeclosure umbrella or the urgent
    timeline opener never fires."""
    flags = digest._distress_flags(["preforeclosure", "noticeOfSale", "vacant"])
    assert flags[0] == "Notice of Sale"


def test_unknown_flags_are_dropped():
    assert digest._distress_flags(["someFutureFlag"]) == []


def test_no_flags_yields_an_empty_list():
    assert digest._distress_flags([]) == []
    assert digest._distress_flags(None) == []


# ---------------------------------------------------------------------------
# Row -> Lead
# ---------------------------------------------------------------------------


def test_row_to_lead_builds_a_readable_address():
    lead = digest.row_to_lead(make_row())
    assert lead.address == "1 Main St, Bethesda, 20814"


def test_row_to_lead_tolerates_missing_address_parts():
    lead = digest.row_to_lead(make_row(street=None, city=None, zip=None))
    assert lead.address == "unknown address"


def test_row_to_lead_defaults_unknown_owner():
    assert digest.row_to_lead(make_row(owner_name=None)).owner_name == "unknown"


def test_row_to_lead_carries_the_preforeclosure_flag():
    assert digest.row_to_lead(make_row()).in_preforeclosure is True
    assert digest.row_to_lead(make_row(quick_lists=["vacant"])).in_preforeclosure is False


def test_row_to_lead_marks_high_priority_above_the_threshold():
    assert digest.row_to_lead(make_row(total_score=84.0)).high_priority is True
    assert digest.row_to_lead(make_row(total_score=41.0)).high_priority is False


def test_row_to_lead_handles_a_row_with_no_financials():
    lead = digest.row_to_lead(
        make_row(estimated_value=None, total_open_lien_balance=None, estimated_profit=None)
    )
    assert lead.assessed_value is None
    assert lead.payoff_profit is None


def test_row_to_lead_uses_the_top_signal_as_motivation():
    lead = digest.row_to_lead(make_row())
    assert lead.motivation_signal == "Notice of Sale"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_text_digest_includes_address_owner_and_call_prep():
    body = digest.build_text([make_row()])
    assert "1 Main St, Bethesda, 20814" in body
    assert "Jane Doe" in body
    assert "Price:" in body  # a PCMT call-prep line made it through


def test_text_digest_flags_un_skip_traced_leads():
    assert "not skip-traced" in digest.build_text([make_row(phone=None)])


def test_html_digest_is_well_formed_and_complete():
    body = digest.build_html([make_row()])
    assert body.startswith("<html>") and body.endswith("</html>")
    assert "1 Main St, Bethesda, 20814" in body
    assert "$500,000" in body
    assert "62%" in body


def test_html_digest_shows_unknown_equity_without_crashing():
    """Regression: a conditional in the f-string chain previously swallowed
    the entire value/owed/spread row when equity_percent was NULL."""
    body = digest.build_html([make_row(equity_percent=None)])
    assert "unknown" in body
    assert "$500,000" in body  # the spread survived


def test_html_digest_surfaces_do_not_call():
    body = digest.build_html([make_row(do_not_call=True)])
    assert "DO NOT CALL" in body


def test_html_digest_escapes_owner_names():
    body = digest.build_html([make_row(owner_name="<script>alert(1)</script>")])
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_digest_renders_every_lead():
    rows = [make_row(batchdata_id=f"id{i}", street=f"{i} Main St") for i in range(5)]
    body = digest.build_text(rows)
    for i in range(5):
        assert f"{i} Main St" in body


def test_money_formatting():
    assert digest._money(1_236_056) == "$1,236,056"
    assert digest._money(None) == "unknown"
