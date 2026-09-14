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
        zillow_link="https://example.com",
    )
    body = cli.build_digest_body([lead])
    assert "LOW MARGIN" in body
    assert "DNC" in body
    assert "Jane Doe" in body
    assert "$300,000" in body  # current value (assessed_value)
    assert "$40,000" in body  # repair cost estimate
    assert "$350,000" in body  # potential ARV
    assert "$205,000" in body  # max allowable offer
    assert "rule-of-thumb" in body
    assert "Distress signals: Vacant, Tax Default" in body


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
