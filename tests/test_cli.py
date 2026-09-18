import sys

import skip_tracer.cli as cli

# Trimmed to the fields cli.py reads, from a real call against 11132
# Willowbrook Dr, Potomac, MD 20854 (see batchdata_client.py's module
# docstring) — locks in the verified response shape as a regression test.
REAL_SKIP_TRACE_RESPONSE = {
    "results": {
        "persons": [
            {
                "name": {"full": "Gunay H Evinch"},
                "phoneNumbers": [{"number": "3013092221", "dnc": True}],
                "emails": [
                    {"email": "gevinch@saltzmanevinch.com", "tested": True},
                    {"email": "egunay1@bellsouth.net", "tested": False},
                    {"email": "gevinch@yahoo.com", "tested": False},
                ],
                "dnc": {"tcpa": False},
                "property": {"owner": {"name": {"full": "Atalay Evinch"}}},
            }
        ]
    }
}

REAL_VALUATION_RESPONSE = {
    "results": {
        "properties": [
            {
                "valuation": {"estimatedValue": 1512714},
                "owner": {"fullName": "Atalay Evinch; Gunay Evinch"},
                "sale": {"lastSale": {"price": 1119500, "saleDate": "2006-10-16T00:00:00.000Z"}},
                "openLien": {"totalOpenLienBalance": 212802},
                "permit": {
                    "permitCount": 2,
                    "latestDate": "2010-07-28T00:00:00.000Z",
                },
                # Real captured quickLists for this property — every
                # distress flag false except highEquity.
                "quickLists": {"vacant": False, "taxDefault": False, "highEquity": True},
            }
        ]
    }
}


class _FakeBatchDataClient:
    def skip_trace(self, address):
        return REAL_SKIP_TRACE_RESPONSE

    def lookup_valuation(self, address):
        return REAL_VALUATION_RESPONSE


def _record(acctid, owner_addr, prop_addr="200 Main St", **extra):
    return {
        "ACCTID": acctid,
        "OWNADD1": owner_addr,
        "OWNADD2": "",
        "ADDRESS": prop_addr,
        **extra,
    }


def test_gather_new_leads_filters_seen_absentee_and_diplomatic(monkeypatch):
    records = [
        _record("1", "100 Other St"),  # genuinely absentee -> kept
        _record("2", "200 Main St"),  # owner-occupied -> dropped
        _record("3", "1 EMBASSY ROW"),  # diplomatic -> dropped
        _record("4", "100 Other St"),  # already seen -> dropped
    ]
    monkeypatch.setattr(cli, "fetch_jurisdiction_leads", lambda jurs: records)
    # classify_owner_entity's non-diplomatic path loads a real NER model;
    # only the diplomatic keyword shortcut (record "3") is exercised for
    # real here, everything else is stubbed to "individual".
    monkeypatch.setattr(
        cli,
        "classify_owner_entity",
        lambda record: "diplomatic" if "EMBASSY" in record["OWNADD1"] else "individual",
    )

    new_records = cli.gather_new_leads(seen={"4"}, jurisdictions=["MONT"])

    assert [r["ACCTID"] for r in new_records] == ["1"]
    assert new_records[0]["entity_type"] == "individual"
    assert new_records[0]["contactability_tier"] == "direct"


def test_gather_new_leads_stops_scanning_once_limit_reached(monkeypatch):
    classify_calls = []

    def fake_classify(record):
        classify_calls.append(record["ACCTID"])
        return "individual"

    records = [_record(str(i), "100 Other St") for i in range(10)]
    monkeypatch.setattr(cli, "fetch_jurisdiction_leads", lambda jurs: records)
    monkeypatch.setattr(cli, "classify_owner_entity", fake_classify)

    new_records = cli.gather_new_leads(seen=set(), jurisdictions=["MONT"], limit=2)

    assert [r["ACCTID"] for r in new_records] == ["0", "1"]
    # classify_owner_entity (the expensive NER call) must not run on the
    # remaining 8 records once the limit is already satisfied.
    assert classify_calls == ["0", "1"]


def test_motivation_signal_flags_non_sale_transfer():
    record = {"CONVEY1": 4, "CONSIDR1": None}
    assert cli._motivation_signal(record) == "Non-sale transfer (possible inheritance)"


def test_motivation_signal_defaults_to_absentee_owner():
    assert cli._motivation_signal({"CONVEY1": 1, "CONSIDR1": 250000}) == "Absentee owner"


def test_enrich_lead_without_batchdata_client_still_builds_zillow_link():
    record = _record("1", "100 Other St", ADDRESS="7924 Lakenheath Way", PREMCITY="Rockville", PREMZIP="20854")
    lead = cli.enrich_lead(record, batchdata=None)

    assert lead.acctid == "1"
    assert "Rockville" in lead.zillow_link
    assert lead.owner_name == "unknown"
    assert lead.condition_tier == "unassessed"


def test_enrich_lead_parses_real_batchdata_response_shapes():
    record = _record(
        "161002618365",
        "11208 Spur Wheel Ln",
        ADDRESS="11132 Willowbrook Dr",
        PREMCITY="Potomac",
        PREMZIP="20854",
        NFMTTLVL=1_481_200,
    )

    lead = cli.enrich_lead(record, batchdata=_FakeBatchDataClient())

    # The deed owner from valuation's owner.fullName wins over skip-trace's
    # property.owner.name.full (both real, valuation's is more complete).
    assert lead.owner_name == "Atalay Evinch; Gunay Evinch"
    assert lead.phone == "3013092221"
    assert lead.do_not_call is True
    assert lead.tcpa_risk is False
    assert lead.arv_estimate == 1512714
    assert lead.low_margin is False  # assessed 1,481,200 < ARV 1,512,714
    assert lead.condition_tier == "unassessed"  # STRUGRAD absent from this record
    assert lead.distress_flags == ["High Equity"]  # only true flag in the real quickLists
    assert lead.email == "gevinch@saltzmanevinch.com"  # tested email preferred over untested
    assert lead.corporate_or_trust_owned is False
    assert lead.last_sale_price == 1119500
    assert lead.last_sale_date == "2006"
    assert lead.total_lien_balance == 212802
    # No repair_cost_estimate (STRUGRAD absent, condition unassessed) means
    # payoff_profit can't be computed either - same gating as mao_estimate.
    assert lead.payoff_profit is None
    assert lead.high_priority is False


def test_distress_flags_only_includes_true_flags_in_label_order():
    quick_lists = {
        "lowEquity": True,
        "vacant": True,
        "taxDefault": False,
        "preforeclosure": True,
        "tiredLandlord": False,
    }
    assert cli._distress_flags(quick_lists) == [
        "Vacant",
        "Pre-Foreclosure",
        "Low Equity",
    ]


def test_distress_flags_empty_when_nothing_true():
    assert cli._distress_flags({"vacant": False, "taxDefault": False}) == []


def test_enrich_lead_skip_trace_owner_used_when_valuation_has_no_properties():
    record = _record("1", "100 Other St", ADDRESS="200 Main St")

    class _NoValuationClient:
        def skip_trace(self, address):
            return REAL_SKIP_TRACE_RESPONSE

        def lookup_valuation(self, address):
            return {"results": {"properties": []}}

    lead = cli.enrich_lead(record, batchdata=_NoValuationClient())

    assert lead.owner_name == "Atalay Evinch"  # skip-trace's property.owner fallback
    assert lead.arv_estimate is None


def test_enrich_lead_computes_repair_cost_and_mao_when_condition_known():
    record = _record(
        "1", "100 Other St", ADDRESS="200 Main St", STRUGRAD="3", SQFTSTRC=1000
    )

    class _LowGradeNoPermitsClient:
        def skip_trace(self, address):
            return {"results": {"persons": [{}]}}

        def lookup_valuation(self, address):
            return {
                "results": {
                    "properties": [{"valuation": {"estimatedValue": 300_000}, "permit": {}}]
                }
            }

    lead = cli.enrich_lead(record, batchdata=_LowGradeNoPermitsClient())

    assert lead.condition_tier == "likely-dated"  # low grade, no recent permits
    assert lead.repair_cost_estimate == 40_000.0  # 1000 sqft * $40/sqft rule-of-thumb
    assert lead.arv_estimate == 300_000
    assert lead.mao_estimate == 300_000 * 0.70 - 40_000


def _client_with_lien(lien_balance=None, free_and_clear=False, arv=300_000):
    quick_lists = {}
    if free_and_clear:
        quick_lists["freeAndClear"] = True
    valuation_properties = {"valuation": {"estimatedValue": arv}, "permit": {}}
    if lien_balance is not None:
        valuation_properties["openLien"] = {"totalOpenLienBalance": lien_balance}
    if quick_lists:
        valuation_properties["quickLists"] = quick_lists

    class _Client:
        def skip_trace(self, address):
            return {"results": {"persons": [{}]}}

        def lookup_valuation(self, address):
            return {"results": {"properties": [valuation_properties]}}

    return _Client()


def test_enrich_lead_flags_high_priority_when_payoff_profit_positive():
    record = _record("1", "100 Other St", ADDRESS="200 Main St", STRUGRAD="3", SQFTSTRC=1000)

    lead = cli.enrich_lead(record, batchdata=_client_with_lien(lien_balance=100_000, arv=300_000))

    # ARV 300k - lien 100k - repair 40k (1000 sqft * $40/sqft) = 160k profit
    assert lead.total_lien_balance == 100_000
    assert lead.payoff_profit == 160_000.0
    assert lead.high_priority is True


def test_enrich_lead_not_high_priority_when_payoff_profit_negative():
    record = _record("1", "100 Other St", ADDRESS="200 Main St", STRUGRAD="3", SQFTSTRC=1000)

    lead = cli.enrich_lead(record, batchdata=_client_with_lien(lien_balance=280_000, arv=300_000))

    # ARV 300k - lien 280k - repair 40k = -20k, not profitable at payoff alone
    assert lead.payoff_profit == -20_000.0
    assert lead.high_priority is False


def test_enrich_lead_free_and_clear_treats_lien_balance_as_zero():
    record = _record("1", "100 Other St", ADDRESS="200 Main St", STRUGRAD="3", SQFTSTRC=1000)

    lead = cli.enrich_lead(
        record, batchdata=_client_with_lien(lien_balance=None, free_and_clear=True, arv=300_000)
    )

    assert lead.total_lien_balance == 0
    assert lead.payoff_profit == 260_000.0  # 300k - 0 - 40k repair
    assert lead.high_priority is True


def test_enrich_lead_no_lien_signal_leaves_payoff_fields_unset():
    record = _record("1", "100 Other St", ADDRESS="200 Main St", STRUGRAD="3", SQFTSTRC=1000)

    lead = cli.enrich_lead(record, batchdata=_client_with_lien(lien_balance=None, arv=300_000))

    assert lead.total_lien_balance is None
    assert lead.payoff_profit is None
    assert lead.high_priority is False


def test_enrich_lead_reuses_cached_batchdata_responses():
    record = _record("1", "100 Other St", ADDRESS="200 Main St")
    call_counts = {"skip_trace": 0, "valuation": 0}

    class _CountingClient:
        def skip_trace(self, address):
            call_counts["skip_trace"] += 1
            return {"results": {"persons": [{}]}}

        def lookup_valuation(self, address):
            call_counts["valuation"] += 1
            return {"results": {"properties": [{"valuation": {"estimatedValue": 100_000}}]}}

    client = _CountingClient()
    cache: dict = {}

    cli.enrich_lead(record, client, cache=cache)
    cli.enrich_lead(record, client, cache=cache)

    assert call_counts == {"skip_trace": 1, "valuation": 1}
    assert cache["1"]["record"] == record


def test_call_script_leads_with_probate_angle_for_inherited_flag():
    lead = cli.Lead(acctid="1", address="1 Main St", distress_flags=["Inherited"])

    lines = cli.call_script(lead)

    assert any("do NOT open with" in line and "we pay cash" in line for line in lines)
    assert any(line.startswith("Price:") for line in lines)
    assert any(line.startswith("Dealbreaker check:") for line in lines)


def test_call_script_leads_with_probate_angle_for_non_sale_transfer_signal():
    lead = cli.Lead(
        acctid="1",
        address="1 Main St",
        motivation_signal="Non-sale transfer (possible inheritance)",
    )

    lines = cli.call_script(lead)

    assert any("do NOT open with" in line for line in lines)


def test_call_script_leads_with_foreclosure_urgency_for_preforeclosure_flag():
    lead = cli.Lead(acctid="1", address="1 Main St", distress_flags=["Pre-Foreclosure"])

    lines = cli.call_script(lead)

    assert any("timeline" in line.lower() for line in lines[:1])


def test_call_script_falls_back_to_general_opener_with_no_signals():
    lead = cli.Lead(acctid="1", address="1 Main St")

    lines = cli.call_script(lead)

    assert lines[0].startswith("Opener: General absentee-owner opener")


def test_call_script_flags_dnc_and_tcpa_as_call_only():
    lead = cli.Lead(acctid="1", address="1 Main St", do_not_call=True)

    lines = cli.call_script(lead)

    assert any("call only, do not text" in line for line in lines)


def test_call_script_notes_no_consent_when_not_dnc_or_tcpa():
    lead = cli.Lead(acctid="1", address="1 Main St")

    lines = cli.call_script(lead)

    assert any("No consent on file for texting" in line for line in lines)


def test_build_digest_body_includes_call_prep_section():
    lead = cli.Lead(
        acctid="1",
        address="1 Main St",
        distress_flags=["Inherited"],
        zillow_link="https://example.com",
    )

    body = cli.build_digest_body([lead])

    assert "Call prep (Price / Condition / Motivation / Time):" in body
    assert "we pay cash" in body


def test_build_digest_body_includes_valuation_and_mao_figures():
    lead = cli.Lead(
        acctid="1",
        address="7924 Lakenheath Way",
        owner_name="Jane Doe",
        phone="555-1234",
        do_not_call=True,
        low_margin=True,
        assessed_value=300_000,
        arv_estimate=350_000,
        repair_cost_estimate=40_000,
        mao_estimate=205_000,
        distress_flags=["Vacant", "Tax Default"],
        last_sale_price=275_000,
        last_sale_date="2015",
        zillow_link="https://example.com",
    )
    body = cli.build_digest_body([lead])
    assert "LOW MARGIN" in body
    assert "DNC" in body
    assert "Jane Doe" in body
    assert "$300,000" in body  # county-assessed value
    assert "$40,000" in body  # repair cost estimate
    assert "$350,000" in body  # BatchData estimated value (AVM)
    assert "$205,000" in body  # max allowable offer
    assert "rule-of-thumb" in body
    assert "Distress signals: Vacant, Tax Default" in body
    assert "Last sale: $275,000 (2015)" in body


def test_build_digest_body_omits_last_sale_line_when_unknown():
    lead = cli.Lead(acctid="1", address="1 Main St", zillow_link="https://example.com")

    body = cli.build_digest_body([lead])

    assert "Last sale" not in body


def test_build_digest_body_marks_high_priority_leads():
    lead = cli.Lead(
        acctid="1",
        address="1 Main St",
        total_lien_balance=100_000,
        payoff_profit=160_000,
        high_priority=True,
        zillow_link="https://example.com",
    )

    body = cli.build_digest_body([lead])

    assert "[HIGH PRIORITY] 1 Main St" in body
    assert "Owes: $100,000" in body
    assert "Profit if offer = payoff: $160,000 — HIGH PRIORITY" in body


def test_build_digest_body_omits_payoff_line_without_lien_data():
    lead = cli.Lead(acctid="1", address="1 Main St", zillow_link="https://example.com")

    body = cli.build_digest_body([lead])

    assert "Owes:" not in body
    assert "[HIGH PRIORITY]" not in body


def test_build_digest_body_shows_unknown_for_missing_dollar_figures():
    lead = cli.Lead(acctid="1", address="1 Main St", zillow_link="https://example.com")

    body = cli.build_digest_body([lead])

    assert "unknown" in body
    assert "Max allowable offer" not in body  # omitted entirely when there's no MAO
    assert "Distress signals" not in body  # omitted entirely when there are none


def _viable_lead(**overrides):
    """A lead that passes every _exclusion_reasons() check by default."""
    defaults = dict(
        acctid="1",
        address="1 Main St",
        phone="555-1234",
        low_margin=False,
        mao_estimate=50_000,
        corporate_or_trust_owned=False,
    )
    return cli.Lead(**{**defaults, **overrides})


def test_exclusion_reasons_empty_for_a_viable_lead():
    assert cli._exclusion_reasons(_viable_lead()) == []


def test_exclusion_reasons_flags_low_margin():
    assert "low margin" in cli._exclusion_reasons(_viable_lead(low_margin=True))[0]


def test_exclusion_reasons_flags_non_positive_mao():
    assert "max allowable offer" in cli._exclusion_reasons(_viable_lead(mao_estimate=0))[0]
    assert "max allowable offer" in cli._exclusion_reasons(_viable_lead(mao_estimate=-500))[0]


def test_exclusion_reasons_ignores_missing_mao():
    # No MAO data at all (e.g. BatchData unavailable) isn't itself a reason
    # to exclude - there's nothing to judge margin against.
    assert cli._exclusion_reasons(_viable_lead(mao_estimate=None)) == []


def test_exclusion_reasons_flags_no_contact_info():
    lead = _viable_lead(phone=None, email=None)
    assert "no phone or email found" in cli._exclusion_reasons(lead)


def test_exclusion_reasons_email_alone_is_sufficient_contact_info():
    lead = _viable_lead(phone=None, email="owner@example.com")
    assert "no phone or email found" not in cli._exclusion_reasons(lead)


def test_exclusion_reasons_flags_corporate_or_trust_owned():
    lead = _viable_lead(corporate_or_trust_owned=True)
    assert "corporate/trust owned" in cli._exclusion_reasons(lead)


def test_filter_worth_pursuing_drops_only_excluded_leads(capsys):
    keep = _viable_lead(acctid="keep")
    drop = _viable_lead(acctid="drop", corporate_or_trust_owned=True)

    kept = cli.filter_worth_pursuing([keep, drop])

    assert kept == [keep]
    assert "Not pursuing drop" in capsys.readouterr().out


def _stub_main_dependencies(monkeypatch, records, passing_acctids):
    """Wires up every cli.main() side-effect except enrich_lead's outcome
    (controlled via passing_acctids) so the enrichment-loop control flow
    can be tested without touching BatchData, Gmail, or state.json/archive
    files for real."""
    monkeypatch.setattr(cli, "load_seen_parcels", lambda: set())
    monkeypatch.setattr(
        cli, "gather_new_leads", lambda seen, jurisdictions, limit: records[:limit]
    )

    enrich_calls = []

    def fake_enrich(record, batchdata, cache=None):
        enrich_calls.append(record["ACCTID"])
        passes = record["ACCTID"] in passing_acctids
        return cli.Lead(
            acctid=record["ACCTID"],
            address=record["ADDRESS"],
            phone="555-0000" if passes else None,
            low_margin=False,
        )

    monkeypatch.setattr(cli, "enrich_lead", fake_enrich)

    monkeypatch.setattr(cli, "load_batchdata_cache", lambda: {})
    monkeypatch.setattr(cli, "save_batchdata_cache", lambda cache: None)

    saved_seen = {}
    monkeypatch.setattr(cli, "save_seen_parcels", lambda seen: saved_seen.update(seen=seen))

    # record_leads() itself is real (pure, no I/O) - only the load/save
    # boundary is mocked, so each starts from {} and its final contents are
    # captured via the save_* calls.
    monkeypatch.setattr(cli, "load_lead_archive", lambda: {})
    saved_archive = {}
    monkeypatch.setattr(cli, "save_lead_archive", lambda arch: saved_archive.update(arch))

    monkeypatch.setattr(cli, "load_qualified_leads", lambda: {})
    saved_qualified = {}
    monkeypatch.setattr(
        cli, "save_qualified_leads", lambda qualified: saved_qualified.update(qualified)
    )

    sent = {}
    monkeypatch.setattr(
        cli,
        "send_weekly_digest",
        lambda leads, to_email: sent.update(leads=leads, to_email=to_email),
    )

    monkeypatch.setenv("DIGEST_EMAIL_TO", "me@example.com")
    monkeypatch.delenv("BATCHDATA_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["skip_tracer.cli"])

    return enrich_calls, saved_seen, saved_archive, saved_qualified, sent


def test_main_stops_enriching_once_target_matches_found(monkeypatch):
    records = [_record(str(i), "100 Other St") for i in range(10)]
    enrich_calls, saved_seen, saved_archive, saved_qualified, sent = _stub_main_dependencies(
        monkeypatch, records, passing_acctids={"0", "1"}
    )
    monkeypatch.setenv("BATCHDATA_MAX_LEADS_PER_RUN", "2")
    monkeypatch.setenv("BATCHDATA_MAX_ENRICHMENT_ATTEMPTS", "10")

    cli.main()

    # Only the 2 candidates needed to satisfy target_matches=2 should ever
    # be enriched (billed) - the other 8 gathered candidates are untouched.
    assert enrich_calls == ["0", "1"]
    assert [lead.acctid for lead in sent["leads"]] == ["0", "1"]
    assert saved_seen["seen"] == {"0", "1"}
    assert list(saved_archive.keys()) == ["0", "1"]
    # Both enriched leads passed, so both land in qualified_leads.json too.
    assert list(saved_qualified.keys()) == ["0", "1"]


def test_main_stops_at_max_attempts_when_match_rate_is_low(monkeypatch, capsys):
    records = [_record(str(i), "100 Other St") for i in range(10)]
    enrich_calls, saved_seen, saved_archive, saved_qualified, sent = _stub_main_dependencies(
        monkeypatch, records, passing_acctids={"9"}  # only the last one passes
    )
    monkeypatch.setenv("BATCHDATA_MAX_LEADS_PER_RUN", "5")
    monkeypatch.setenv("BATCHDATA_MAX_ENRICHMENT_ATTEMPTS", "10")

    cli.main()

    # All 10 gathered candidates get tried (the safety cap), but only 1
    # ever passes - target_matches=5 is never reached.
    assert enrich_calls == [str(i) for i in range(10)]
    assert [lead.acctid for lead in sent["leads"]] == ["9"]
    assert saved_seen["seen"] == set(str(i) for i in range(10))
    assert "Only found 1 of 5 worth pursuing" in capsys.readouterr().out
    # All 10 tried candidates are archived, but only the 1 that passed
    # lands in qualified_leads.json.
    assert len(saved_archive) == 10
    assert list(saved_qualified.keys()) == ["9"]


def test_main_enrichment_attempts_floor_is_at_least_target_matches(monkeypatch):
    records = [_record(str(i), "100 Other St") for i in range(5)]
    enrich_calls, *_ = _stub_main_dependencies(monkeypatch, records, passing_acctids=set())
    # A misconfigured cap smaller than the target shouldn't make it
    # impossible to ever reach the target even with a 100% hit rate.
    monkeypatch.setenv("BATCHDATA_MAX_LEADS_PER_RUN", "5")
    monkeypatch.setenv("BATCHDATA_MAX_ENRICHMENT_ATTEMPTS", "1")

    cli.main()

    assert len(enrich_calls) == 5  # raised to match target_matches, not left at 1


def test_main_sorts_high_priority_leads_first(monkeypatch):
    records = [_record(str(i), "100 Other St") for i in range(3)]
    enrich_calls, saved_seen, saved_archive, saved_qualified, sent = _stub_main_dependencies(
        monkeypatch, records, passing_acctids={"0", "1", "2"}
    )

    def fake_enrich(record, batchdata, cache=None):
        enrich_calls.append(record["ACCTID"])
        return cli.Lead(
            acctid=record["ACCTID"],
            address=record["ADDRESS"],
            phone="555-0000",
            low_margin=False,
            high_priority=(record["ACCTID"] == "1"),  # only the middle one
        )

    monkeypatch.setattr(cli, "enrich_lead", fake_enrich)
    monkeypatch.setenv("BATCHDATA_MAX_LEADS_PER_RUN", "3")
    monkeypatch.setenv("BATCHDATA_MAX_ENRICHMENT_ATTEMPTS", "3")

    cli.main()

    assert [lead.acctid for lead in sent["leads"]] == ["1", "0", "2"]
