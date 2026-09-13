from skip_tracer.filtering import (
    contactability_tier,
    is_genuinely_absentee,
    normalize_address,
    classify_owner_entity,
)


def test_normalize_address_collapses_whitespace_and_case():
    assert normalize_address("  123  Main   street ") == "123 MAIN ST"


def test_normalize_address_handles_empty_string():
    assert normalize_address("") == ""


def test_normalize_address_maps_common_suffix_variants():
    assert normalize_address("456 Oak Lane") == "456 OAK LN"
    assert normalize_address("789 Elm Drive") == "789 ELM DR"


def test_is_genuinely_absentee_true_for_different_addresses():
    record = {"OWNADD1": "100 Other St", "ADDRESS": "200 Main St"}
    assert is_genuinely_absentee(record) is True


def test_is_genuinely_absentee_false_when_addresses_match_after_normalization():
    record = {"OWNADD1": "200 Main Street", "ADDRESS": "200 Main St"}
    assert is_genuinely_absentee(record) is False


def test_is_genuinely_absentee_false_when_owner_address_missing():
    record = {"OWNADD1": "", "ADDRESS": "200 Main St"}
    assert is_genuinely_absentee(record) is False


def test_contactability_tier_flags_co_mail_handler():
    assert contactability_tier({"OWNADD1": "C/O John Smith"}) == "third_party_mail_handler"
    assert contactability_tier({"OWNADD1": "CO John Smith"}) == "third_party_mail_handler"


def test_contactability_tier_direct_otherwise():
    assert contactability_tier({"OWNADD1": "100 Other St"}) == "direct"


def test_classify_owner_entity_diplomatic_shortcut_skips_model_load():
    record = {"OWNADD1": "1234 EMBASSY ROW", "OWNADD2": ""}
    assert classify_owner_entity(record) == "diplomatic"


def test_classify_owner_entity_abbreviated_embassy_keyword():
    record = {"OWNADD1": "1 EMBSY DR", "OWNADD2": ""}
    assert classify_owner_entity(record) == "diplomatic"


def test_classify_owner_entity_empty_text_defaults_individual():
    assert classify_owner_entity({"OWNADD1": "", "OWNADD2": ""}) == "individual"
