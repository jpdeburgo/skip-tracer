"""Postgres persistence for BatchData property-search results.

Why a database at all: BatchData's property/search endpoint is a paid bulk
call that returns at most 25 properties per request (confirmed live — see
batchdata_client.search_properties()), while reporting a much larger
server-side match count. Montgomery County alone reported 474
preforeclosure matches, so building a usable Maryland lead list means
paginating with `skip` across many billable calls. Throwing those results
away between runs — or re-pulling them because they only lived in a local
gitignored JSON file on one laptop — is the single most expensive mistake
this pipeline can make.

So every paid response is written to Render Postgres exactly once and
re-read for free forever after:

  batchdata_search_runs  one row per paid API call (audit + cooldown)
  properties             one row per distinct property ever returned
  leads                  scored, actionable, human-workable lead records

`leads` is deliberately separate from `properties`: `properties` is an
immutable-ish record of what we paid BatchData for, while `leads` carries
mutable human workflow state (call status, notes, outcome) that must
survive re-importing or re-scoring the underlying property. Re-running a
search updates `properties` but never clobbers a lead's call history.

Connection comes from DATABASE_URL (Render's external connection string
locally, internal on Render itself). Nothing here assumes the Render CLI
is installed or authenticated — it's a plain libpq URL.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# BatchData caps every property/search response at this many properties
# regardless of the requested `take` — paginate with `skip` to exceed it.
BATCHDATA_PAGE_SIZE = 25

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS batchdata_search_runs (
        id             BIGSERIAL PRIMARY KEY,
        query          TEXT        NOT NULL,
        quicklist      TEXT        NOT NULL,
        skip           INTEGER     NOT NULL DEFAULT 0,
        take           INTEGER     NOT NULL,
        requested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        results_found  INTEGER,
        result_count   INTEGER,
        error          TEXT,
        raw_response   JSONB
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS batchdata_search_runs_cooldown_idx
        ON batchdata_search_runs (query, quicklist, requested_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS properties (
        batchdata_id            TEXT PRIMARY KEY,
        search_run_id           BIGINT REFERENCES batchdata_search_runs (id),
        first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        street                  TEXT,
        city                    TEXT,
        county                  TEXT,
        state                   TEXT,
        zip                     TEXT,
        latitude                DOUBLE PRECISION,
        longitude               DOUBLE PRECISION,
        owner_name              TEXT,
        owner_occupied          BOOLEAN,
        owner_status_type       TEXT,
        owner_mailing_street    TEXT,
        owner_mailing_city      TEXT,
        owner_mailing_state     TEXT,
        owner_mailing_zip       TEXT,
        year_built              INTEGER,
        bedroom_count           INTEGER,
        bathroom_count          NUMERIC,
        building_sqft           INTEGER,
        estimated_value         NUMERIC,
        price_range_min         NUMERIC,
        price_range_max         NUMERIC,
        confidence_score        INTEGER,
        equity_balance          NUMERIC,
        ltv                     NUMERIC,
        equity_percent          NUMERIC,
        open_lien_count         INTEGER,
        total_open_lien_balance NUMERIC,
        involuntary_lien_total  NUMERIC,
        last_sale_price         NUMERIC,
        last_sale_date          DATE,
        quick_lists             TEXT[] NOT NULL DEFAULT '{}',
        raw                     JSONB  NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS properties_state_idx ON properties (state)
    """,
    """
    CREATE INDEX IF NOT EXISTS properties_quick_lists_idx
        ON properties USING GIN (quick_lists)
    """,
    """
    CREATE TABLE IF NOT EXISTS leads (
        id                 BIGSERIAL PRIMARY KEY,
        batchdata_id       TEXT NOT NULL UNIQUE
                           REFERENCES properties (batchdata_id) ON DELETE CASCADE,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        estimated_profit   NUMERIC,
        profit_score       NUMERIC,
        motivation_score   NUMERIC,
        contactability_score NUMERIC,
        total_score        NUMERIC,
        score_breakdown    JSONB,
        disqualified       BOOLEAN NOT NULL DEFAULT FALSE,
        disqualified_reason TEXT,
        status             TEXT NOT NULL DEFAULT 'new',
        phone              TEXT,
        email              TEXT,
        do_not_call        BOOLEAN,
        last_contacted_at  TIMESTAMPTZ,
        notes              TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS leads_score_idx
        ON leads (total_score DESC NULLS LAST)
    """,
    """
    CREATE INDEX IF NOT EXISTS leads_status_idx ON leads (status)
    """,
    # A view rather than a table: "Maryland leads that fit our criteria" is a
    # derived question, and a view can never drift out of sync with the
    # properties/leads it summarizes the way a copied table would.
    """
    CREATE OR REPLACE VIEW maryland_leads AS
        SELECT
            l.id,
            l.batchdata_id,
            p.street,
            p.city,
            p.county,
            p.zip,
            p.owner_name,
            p.owner_occupied,
            p.estimated_value,
            p.total_open_lien_balance,
            p.equity_percent,
            l.estimated_profit,
            l.profit_score,
            l.motivation_score,
            l.contactability_score,
            l.total_score,
            l.status,
            l.phone,
            l.do_not_call,
            l.last_contacted_at,
            p.quick_lists
        FROM leads l
        JOIN properties p ON p.batchdata_id = l.batchdata_id
        WHERE p.state = 'MD'
          AND l.disqualified = FALSE
        ORDER BY l.total_score DESC NULLS LAST
    """,
)

VALID_LEAD_STATUSES = frozenset(
    {"new", "queued", "called", "contacted", "appointment", "offer_made", "dead", "under_contract"}
)


def get_database_url(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else dict(os.environ)
    url = env.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set — copy Render's external connection string "
            "for the property_catalog database into .env (it is gitignored)."
        )
    return url


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    """Yields a dict-row connection, committing on clean exit."""
    with psycopg.connect(url or get_database_url(), row_factory=dict_row) as conn:
        yield conn


def init_schema(conn: psycopg.Connection) -> None:
    """Idempotent — safe to run on every deploy."""
    with conn.cursor() as cur:
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)
    conn.commit()


# --------------------------------------------------------------------------
# Search-run CRUD (also the durable backing store for the monthly cooldown)
# --------------------------------------------------------------------------


def record_search_run(
    conn: psycopg.Connection,
    query: str,
    quicklist: str,
    skip: int,
    take: int,
    response: dict[str, Any] | None,
    error: str | None = None,
) -> int:
    """Writes one paid API call to the audit table, returning its id.

    Called for failures too (`response=None`, `error` set): a 403
    "insufficient balance" or a timeout still consumed a request cycle, and
    recording it is what stops a retry loop from quietly burning the
    wallet.
    """
    meta_results = ((response or {}).get("results", {}).get("meta", {}) or {}).get("results", {})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO batchdata_search_runs
                (query, quicklist, skip, take, results_found, result_count, error, raw_response)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                query,
                quicklist,
                skip,
                take,
                meta_results.get("resultsFound"),
                meta_results.get("resultCount"),
                error,
                Jsonb(response) if response is not None else None,
            ),
        )
        run_id = cur.fetchone()["id"]
    conn.commit()
    return run_id


def last_successful_search(
    conn: psycopg.Connection, query: str, quicklist: str
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, requested_at, results_found, result_count
            FROM batchdata_search_runs
            WHERE query = %s AND quicklist = %s AND error IS NULL
            ORDER BY requested_at DESC
            LIMIT 1
            """,
            (query, quicklist),
        )
        return cur.fetchone()


def is_in_cooldown(
    conn: psycopg.Connection, query: str, quicklist: str, cooldown_days: int = 30
) -> tuple[bool, datetime | None]:
    """True when this (query, quicklist) was pulled within `cooldown_days`.

    Checks only *successful* runs, so a failed call doesn't lock out a
    legitimate retry for a month — the failure is still recorded for
    auditing, it just doesn't count as "we already have this data."
    """
    last = last_successful_search(conn, query, quicklist)
    if last is None:
        return False, None
    age = datetime.now(timezone.utc) - last["requested_at"]
    return age < timedelta(days=cooldown_days), last["requested_at"]


# --------------------------------------------------------------------------
# Property CRUD
# --------------------------------------------------------------------------


def _get(mapping: Any, *path: str) -> Any:
    current = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _true_quick_lists(prop: dict[str, Any]) -> list[str]:
    quick_lists = prop.get("quickLists") or {}
    return sorted(key for key, value in quick_lists.items() if value is True)


def _involuntary_lien_total(prop: dict[str, Any]) -> float | None:
    liens = _get(prop, "involuntaryLien", "liens")
    if not liens:
        return None
    return sum(lien.get("lienAmount") or 0 for lien in liens)


def upsert_property(
    conn: psycopg.Connection, prop: dict[str, Any], search_run_id: int | None = None
) -> str:
    """Inserts or refreshes one property/search result row.

    On conflict this refreshes the market/lien data and `last_seen_at` but
    deliberately leaves `first_seen_at` alone — how long a property has
    been sitting in our catalog is itself a signal (a preforeclosure that
    keeps reappearing month after month is an owner who hasn't solved
    their problem yet).
    """
    batchdata_id = prop.get("_id")
    if not batchdata_id:
        raise ValueError("property is missing its BatchData _id")

    values = (
        batchdata_id,
        search_run_id,
        _get(prop, "address", "street"),
        _get(prop, "address", "city"),
        _get(prop, "address", "county"),
        _get(prop, "address", "state"),
        _get(prop, "address", "zip"),
        _get(prop, "address", "latitude"),
        _get(prop, "address", "longitude"),
        _get(prop, "owner", "fullName"),
        _get(prop, "owner", "ownerOccupied"),
        _get(prop, "owner", "ownerStatusType"),
        _get(prop, "owner", "mailingAddress", "street"),
        _get(prop, "owner", "mailingAddress", "city"),
        _get(prop, "owner", "mailingAddress", "state"),
        _get(prop, "owner", "mailingAddress", "zip"),
        _get(prop, "building", "yearBuilt"),
        _get(prop, "building", "bedroomCount"),
        _get(prop, "building", "bathroomCount"),
        _get(prop, "building", "livingAreaSquareFeet"),
        _get(prop, "valuation", "estimatedValue"),
        _get(prop, "valuation", "priceRangeMin"),
        _get(prop, "valuation", "priceRangeMax"),
        _get(prop, "valuation", "confidenceScore"),
        _get(prop, "valuation", "equityCurrentEstimatedBalance"),
        _get(prop, "valuation", "ltv"),
        _get(prop, "valuation", "equityPercent"),
        _get(prop, "openLien", "totalOpenLienCount"),
        _get(prop, "openLien", "totalOpenLienBalance"),
        _involuntary_lien_total(prop),
        _get(prop, "sale", "lastSale", "price"),
        _get(prop, "sale", "lastSale", "saleDate"),
        _true_quick_lists(prop),
        Jsonb(prop),
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO properties (
                batchdata_id, search_run_id, street, city, county, state, zip,
                latitude, longitude, owner_name, owner_occupied, owner_status_type,
                owner_mailing_street, owner_mailing_city, owner_mailing_state,
                owner_mailing_zip, year_built, bedroom_count, bathroom_count,
                building_sqft, estimated_value, price_range_min, price_range_max,
                confidence_score, equity_balance, ltv, equity_percent,
                open_lien_count, total_open_lien_balance, involuntary_lien_total,
                last_sale_price, last_sale_date, quick_lists, raw
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s
            )
            ON CONFLICT (batchdata_id) DO UPDATE SET
                last_seen_at            = NOW(),
                search_run_id           = EXCLUDED.search_run_id,
                estimated_value         = EXCLUDED.estimated_value,
                price_range_min         = EXCLUDED.price_range_min,
                price_range_max         = EXCLUDED.price_range_max,
                confidence_score        = EXCLUDED.confidence_score,
                equity_balance          = EXCLUDED.equity_balance,
                ltv                     = EXCLUDED.ltv,
                equity_percent          = EXCLUDED.equity_percent,
                open_lien_count         = EXCLUDED.open_lien_count,
                total_open_lien_balance = EXCLUDED.total_open_lien_balance,
                involuntary_lien_total  = EXCLUDED.involuntary_lien_total,
                owner_name              = EXCLUDED.owner_name,
                owner_occupied          = EXCLUDED.owner_occupied,
                quick_lists             = EXCLUDED.quick_lists,
                raw                     = EXCLUDED.raw
            """,
            values,
        )
    conn.commit()
    return batchdata_id


def import_search_response(
    conn: psycopg.Connection,
    response: dict[str, Any],
    query: str,
    quicklist: str,
    skip: int = 0,
    take: int = BATCHDATA_PAGE_SIZE,
) -> tuple[int, list[str]]:
    """Records the paid call and upserts every property it returned."""
    run_id = record_search_run(conn, query, quicklist, skip, take, response)
    properties = (response.get("results") or {}).get("properties") or []
    return run_id, [upsert_property(conn, prop, run_id) for prop in properties]


def get_property(conn: psycopg.Connection, batchdata_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM properties WHERE batchdata_id = %s", (batchdata_id,))
        return cur.fetchone()


def list_properties(
    conn: psycopg.Connection,
    state: str | None = None,
    quicklist: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if state:
        clauses.append("state = %s")
        params.append(state)
    if quicklist:
        clauses.append("%s = ANY(quick_lists)")
        params.append(quicklist)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM properties {where} ORDER BY last_seen_at DESC"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def delete_property(conn: psycopg.Connection, batchdata_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM properties WHERE batchdata_id = %s", (batchdata_id,))
        deleted = cur.rowcount > 0
    conn.commit()
    return deleted


def count_properties(conn: psycopg.Connection, state: str | None = None) -> int:
    with conn.cursor() as cur:
        if state:
            cur.execute("SELECT COUNT(*) AS n FROM properties WHERE state = %s", (state,))
        else:
            cur.execute("SELECT COUNT(*) AS n FROM properties")
        return cur.fetchone()["n"]


# --------------------------------------------------------------------------
# Lead CRUD
# --------------------------------------------------------------------------


def upsert_lead(
    conn: psycopg.Connection, batchdata_id: str, score: dict[str, Any]
) -> int:
    """Writes a scored lead, preserving human workflow state on re-score.

    `status`, `notes`, `last_contacted_at`, `phone`, `email` are NOT
    touched on conflict — re-running the scorer after a fresh monthly pull
    must never erase the record of a call that already happened.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO leads (
                batchdata_id, estimated_profit, profit_score, motivation_score,
                contactability_score, total_score, score_breakdown,
                disqualified, disqualified_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (batchdata_id) DO UPDATE SET
                updated_at           = NOW(),
                estimated_profit     = EXCLUDED.estimated_profit,
                profit_score         = EXCLUDED.profit_score,
                motivation_score     = EXCLUDED.motivation_score,
                contactability_score = EXCLUDED.contactability_score,
                total_score          = EXCLUDED.total_score,
                score_breakdown      = EXCLUDED.score_breakdown,
                disqualified         = EXCLUDED.disqualified,
                disqualified_reason  = EXCLUDED.disqualified_reason
            RETURNING id
            """,
            (
                batchdata_id,
                score.get("estimated_profit"),
                score.get("profit_score"),
                score.get("motivation_score"),
                score.get("contactability_score"),
                score.get("total_score"),
                Jsonb(score.get("breakdown") or {}),
                score.get("disqualified", False),
                score.get("disqualified_reason"),
            ),
        )
        lead_id = cur.fetchone()["id"]
    conn.commit()
    return lead_id


def get_lead(conn: psycopg.Connection, lead_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM leads WHERE id = %s", (lead_id,))
        return cur.fetchone()


def top_maryland_leads(conn: psycopg.Connection, limit: int = 25) -> list[dict[str, Any]]:
    """The weekly/monthly call list, best-scoring first."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM maryland_leads LIMIT %s", (limit,))
        return cur.fetchall()


def update_lead_status(
    conn: psycopg.Connection,
    lead_id: int,
    status: str,
    notes: str | None = None,
    mark_contacted: bool = False,
) -> bool:
    if status not in VALID_LEAD_STATUSES:
        raise ValueError(
            f"unknown lead status {status!r} — expected one of {sorted(VALID_LEAD_STATUSES)}"
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE leads
            SET status            = %s,
                notes             = COALESCE(%s, notes),
                last_contacted_at = CASE WHEN %s THEN NOW() ELSE last_contacted_at END,
                updated_at        = NOW()
            WHERE id = %s
            """,
            (status, notes, mark_contacted, lead_id),
        )
        updated = cur.rowcount > 0
    conn.commit()
    return updated


def set_lead_contact_info(
    conn: psycopg.Connection,
    lead_id: int,
    phone: str | None = None,
    email: str | None = None,
    do_not_call: bool | None = None,
) -> bool:
    """Backfills skip-trace results onto a lead without re-scoring it."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE leads
            SET phone       = COALESCE(%s, phone),
                email       = COALESCE(%s, email),
                do_not_call = COALESCE(%s, do_not_call),
                updated_at  = NOW()
            WHERE id = %s
            """,
            (phone, email, do_not_call, lead_id),
        )
        updated = cur.rowcount > 0
    conn.commit()
    return updated


def delete_lead(conn: psycopg.Connection, lead_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM leads WHERE id = %s", (lead_id,))
        deleted = cur.rowcount > 0
    conn.commit()
    return deleted
