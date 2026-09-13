import skip_tracer.cli as cli


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


def test_build_digest_body_includes_low_margin_and_dnc_flags():
    lead = cli.Lead(
        acctid="1",
        address="7924 Lakenheath Way",
        owner_name="Jane Doe",
        phone="555-1234",
        do_not_call=True,
        low_margin=True,
        zillow_link="https://example.com",
    )
    body = cli.build_digest_body([lead])
    assert "LOW MARGIN" in body
    assert "DNC" in body
    assert "Jane Doe" in body
