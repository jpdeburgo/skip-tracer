"""Tests for the Postgres persistence layer.

These use a fake connection rather than a live database so the suite stays
runnable without DATABASE_URL. They cover the pure logic (field extraction,
cooldown arithmetic, status validation) plus the SQL contracts that protect
paid data -- particularly that re-importing never destroys call history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from skip_tracer import database


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        self._result = self.conn.results.pop(0) if self.conn.results else None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result if isinstance(self._result, list) else []

    @property
    def rowcount(self):
        return self.conn.rowcount


class FakeConnection:
    def __init__(self, results=None, rowcount=1):
        self.executed = []
        self.results = list(results or [])
        self.rowcount = rowcount
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


def sample_api_property(**overrides):
    prop = {
        "_id": "abc123",
        "address": {
            "street": "1 Main St",
            "city": "Baltimore",
            "county": "Baltimore City",
            "state": "MD",
            "zip": "21201",
        },
        "owner": {
            "fullName": "Jane Doe",
            "ownerOccupied": True,
            "mailingAddress": {"street": "1 Main St", "city": "Baltimore"},
        },
        "valuation": {"estimatedValue": 400_000, "equityPercent": 50},
        "openLien": {"totalOpenLienBalance": 200_000, "totalOpenLienCount": 1},
        "quickLists": {"preforeclosure": True, "vacant": False, "noticeOfSale": True},
    }
    prop.update(overrides)
    return prop


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_get_database_url_reads_the_environment():
    assert database.get_database_url({"DATABASE_URL": "postgres://x"}) == "postgres://x"


def test_get_database_url_explains_itself_when_unset():
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        database.get_database_url({})


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------


def test_get_walks_nested_keys():
    assert database._get({"a": {"b": {"c": 1}}}, "a", "b", "c") == 1


def test_get_returns_none_for_a_missing_branch():
    assert database._get({"a": {}}, "a", "b", "c") is None


def test_get_survives_a_non_dict_midway():
    """Real BatchData payloads sometimes put a scalar where a sub-object is
    expected; that must not raise mid-import and abort the whole batch."""
    assert database._get({"a": 5}, "a", "b") is None


def test_true_quick_lists_keeps_only_true_flags():
    prop = sample_api_property()
    assert database._true_quick_lists(prop) == ["noticeOfSale", "preforeclosure"]


def test_true_quick_lists_is_empty_when_absent():
    assert database._true_quick_lists({}) == []


def test_involuntary_lien_total_sums_the_liens():
    prop = {"involuntaryLien": {"liens": [{"lienAmount": 1000}, {"lienAmount": 2500}]}}
    assert database._involuntary_lien_total(prop) == 3500


def test_involuntary_lien_total_is_none_without_liens():
    assert database._involuntary_lien_total({}) is None


def test_involuntary_lien_total_tolerates_a_missing_amount():
    prop = {"involuntaryLien": {"liens": [{"lienAmount": 1000}, {}]}}
    assert database._involuntary_lien_total(prop) == 1000


# ---------------------------------------------------------------------------
# Property upsert
# ---------------------------------------------------------------------------


def test_upsert_property_requires_a_batchdata_id():
    with pytest.raises(ValueError, match="_id"):
        database.upsert_property(FakeConnection(), {"address": {}})


def test_upsert_property_preserves_first_seen_at():
    """How long a property has been in the catalog is itself a signal: a
    preforeclosure reappearing month after month is an unsolved problem."""
    conn = FakeConnection()
    database.upsert_property(conn, sample_api_property())
    sql = conn.executed[0][0]
    assert "ON CONFLICT (batchdata_id) DO UPDATE" in sql
    assert "last_seen_at = NOW()" in sql
    assert "first_seen_at" not in sql.split("DO UPDATE")[1]


def test_upsert_property_returns_the_id_and_commits():
    conn = FakeConnection()
    assert database.upsert_property(conn, sample_api_property()) == "abc123"
    assert conn.commits == 1


def test_import_search_response_records_the_run_and_every_property():
    conn = FakeConnection(results=[{"id": 42}])
    response = {
        "results": {
            "properties": [sample_api_property(), sample_api_property(_id="def456")],
            "meta": {"results": {"resultsFound": 3986, "resultCount": 2}},
        }
    }
    run_id, ids = database.import_search_response(conn, response, "Maryland", "preforeclosure")
    assert run_id == 42
    assert ids == ["abc123", "def456"]


def test_import_search_response_handles_an_empty_page():
    conn = FakeConnection(results=[{"id": 7}])
    run_id, ids = database.import_search_response(conn, {"results": {}}, "Maryland", "preforeclosure")
    assert run_id == 7
    assert ids == []


def test_record_search_run_persists_failures_too():
    """A 403 still consumed a request cycle. Recording it is what keeps a
    retry loop from quietly draining the wallet."""
    conn = FakeConnection(results=[{"id": 9}])
    run_id = database.record_search_run(
        conn, "Maryland", "preforeclosure", 0, 25, None, error="Insufficient balance."
    )
    assert run_id == 9
    params = conn.executed[0][1]
    assert "Insufficient balance." in params


def test_record_search_run_separates_results_found_from_result_count():
    """resultsFound is a server-side COUNT, resultCount is what we actually
    received and paid to transmit. Conflating them once caused a false
    belief that 279k properties had been downloaded."""
    conn = FakeConnection(results=[{"id": 1}])
    response = {"results": {"meta": {"results": {"resultsFound": 279621, "resultCount": 25}}}}
    database.record_search_run(conn, "q", "preforeclosure", 0, 25, response)
    params = conn.executed[0][1]
    assert 279621 in params and 25 in params


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_no_cooldown_when_never_searched():
    conn = FakeConnection(results=[None])
    in_cooldown, when = database.is_in_cooldown(conn, "Maryland", "preforeclosure")
    assert in_cooldown is False and when is None


def test_cooldown_active_for_a_recent_run():
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    conn = FakeConnection(results=[{"requested_at": recent}])
    in_cooldown, _ = database.is_in_cooldown(conn, "Maryland", "preforeclosure")
    assert in_cooldown is True


def test_cooldown_expires_after_the_window():
    old = datetime.now(timezone.utc) - timedelta(days=31)
    conn = FakeConnection(results=[{"requested_at": old}])
    in_cooldown, _ = database.is_in_cooldown(conn, "Maryland", "preforeclosure")
    assert in_cooldown is False


def test_cooldown_only_counts_successful_runs():
    """A failed call must not lock out a legitimate retry for a month."""
    conn = FakeConnection(results=[None])
    database.last_successful_search(conn, "Maryland", "preforeclosure")
    assert "error IS NULL" in conn.executed[0][0]


# ---------------------------------------------------------------------------
# Lead CRUD
# ---------------------------------------------------------------------------


def test_upsert_lead_preserves_human_workflow_state():
    """The central safety property: re-scoring after a monthly pull must
    never erase a call that already happened."""
    conn = FakeConnection(results=[{"id": 3}])
    database.upsert_lead(conn, "abc123", {"total_score": 80.0})
    update_clause = conn.executed[0][0].split("DO UPDATE")[1]
    for preserved in ("status", "notes", "phone", "email", "last_contacted_at"):
        assert preserved not in update_clause


def test_upsert_lead_refreshes_every_score_component():
    conn = FakeConnection(results=[{"id": 3}])
    database.upsert_lead(conn, "abc123", {"total_score": 80.0})
    update_clause = conn.executed[0][0].split("DO UPDATE")[1]
    for refreshed in ("total_score", "motivation_score", "profit_score", "disqualified"):
        assert refreshed in update_clause


def test_update_lead_status_rejects_an_unknown_status():
    with pytest.raises(ValueError, match="unknown lead status"):
        database.update_lead_status(FakeConnection(), 1, "definitely_not_a_status")


@pytest.mark.parametrize("status", sorted(database.VALID_LEAD_STATUSES))
def test_update_lead_status_accepts_every_declared_status(status):
    assert database.update_lead_status(FakeConnection(rowcount=1), 1, status) is True


def test_update_lead_status_reports_a_missing_lead():
    assert database.update_lead_status(FakeConnection(rowcount=0), 999, "called") is False


def test_update_lead_status_only_stamps_contact_when_asked():
    conn = FakeConnection(rowcount=1)
    database.update_lead_status(conn, 1, "called", mark_contacted=False)
    assert False in conn.executed[0][1]


def test_set_lead_contact_info_does_not_clobber_with_nulls():
    """COALESCE means setting only a phone can't wipe an existing email."""
    conn = FakeConnection(rowcount=1)
    database.set_lead_contact_info(conn, 1, phone="240-555-0134")
    sql = conn.executed[0][0]
    assert "COALESCE(%s, phone)" in sql and "COALESCE(%s, email)" in sql


def test_delete_lead_reports_whether_anything_was_removed():
    assert database.delete_lead(FakeConnection(rowcount=1), 1) is True
    assert database.delete_lead(FakeConnection(rowcount=0), 1) is False


def test_delete_property_reports_whether_anything_was_removed():
    assert database.delete_property(FakeConnection(rowcount=1), "abc") is True
    assert database.delete_property(FakeConnection(rowcount=0), "abc") is False


def test_list_properties_filters_by_state_and_quicklist():
    conn = FakeConnection(results=[[]])
    database.list_properties(conn, state="MD", quicklist="preforeclosure", limit=10)
    sql, params = conn.executed[0]
    assert "state = %s" in sql and "%s = ANY(quick_lists)" in sql
    assert params == ["MD", "preforeclosure", 10]


def test_list_properties_without_filters_has_no_where_clause():
    conn = FakeConnection(results=[[]])
    database.list_properties(conn)
    assert "WHERE" not in conn.executed[0][0]


def test_top_maryland_leads_reads_the_view():
    """A view can't drift from the properties it summarizes the way a
    copied table would."""
    conn = FakeConnection(results=[[]])
    database.top_maryland_leads(conn, limit=25)
    assert "FROM maryland_leads" in conn.executed[0][0]


def test_maryland_view_excludes_disqualified_and_sorts_by_score():
    view_sql = next(s for s in database.SCHEMA_STATEMENTS if "CREATE OR REPLACE VIEW" in s)
    assert "l.disqualified = FALSE" in view_sql
    assert "p.state = 'MD'" in view_sql
    assert "ORDER BY l.total_score DESC" in view_sql


def test_leads_cascade_when_a_property_is_deleted():
    leads_ddl = next(s for s in database.SCHEMA_STATEMENTS if "CREATE TABLE IF NOT EXISTS leads" in s)
    assert "ON DELETE CASCADE" in leads_ddl
