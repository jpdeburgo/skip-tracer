from dataclasses import dataclass

import skip_tracer.archive as archive


@dataclass
class _FakeLead:
    acctid: str
    address: str
    owner_name: str = "unknown"


def test_load_lead_archive_missing_file_returns_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_ARCHIVE_PATH", str(tmp_path / "leads_archive.json"))
    assert archive.load_lead_archive() == {}


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_ARCHIVE_PATH", str(tmp_path / "leads_archive.json"))

    archive.save_lead_archive({"1": {"address": "100 Main St"}})

    assert archive.load_lead_archive() == {"1": {"address": "100 Main St"}}


def test_record_leads_adds_entries_keyed_by_acctid_with_timestamp():
    leads = [
        _FakeLead(acctid="1", address="100 Main St"),
        _FakeLead(acctid="2", address="200 Oak Ave"),
    ]

    result = archive.record_leads({}, leads)

    assert set(result.keys()) == {"1", "2"}
    assert result["1"]["address"] == "100 Main St"
    assert "archived_at" in result["1"]


def test_record_leads_preserves_existing_entries_not_in_this_batch():
    existing = {"9": {"address": "900 Old Rd", "archived_at": "2020-01-01T00:00:00+00:00"}}

    result = archive.record_leads(existing, [_FakeLead(acctid="1", address="100 Main St")])

    assert "9" in result  # untouched from a previous run
    assert "1" in result


def test_record_leads_overwrites_an_existing_acctid_entry():
    existing = {"1": {"address": "stale data", "archived_at": "2020-01-01T00:00:00+00:00"}}

    result = archive.record_leads(existing, [_FakeLead(acctid="1", address="fresh data")])

    assert result["1"]["address"] == "fresh data"
