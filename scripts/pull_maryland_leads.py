"""Monthly Maryland distressed-lead pull — the one billable bulk call.

This is the production entry point for refreshing the lead catalog. It is
deliberately conservative about money:

  1. Checks the database cooldown first and refuses to run if this exact
     (query, quicklist) pull succeeded within --cooldown-days. The check
     lives in Postgres, not a local file, so it holds across machines and
     across a Render cron container that gets a fresh disk every run.
  2. Saves the raw JSON response to disk BEFORE any parsing, so a paid
     response can never be lost to a downstream bug.
  3. Imports into Postgres, upserting by BatchData `_id` so re-running
     never duplicates or double-counts.

Why quicklist defaults to `preforeclosure`: measured against the 50
properties already in the catalog, `preforeclosure` is the umbrella flag
for the whole foreclosure pipeline -- every noticeOfSale (27),
noticeOfLisPendens (15), noticeOfDefault (8), activeAuction (4) and
taxDefault (2) property also carried `preforeclosure`. One filter
therefore captures notice-of-default through scheduled-auction without
spending a call per signal.

IMPORTANT COST NOTE: BatchData caps every property/search response at 25
properties regardless of `take` (confirmed live). One run of this script
returns at most 25 properties. Covering an entire state means paginating
with --skip across many billable calls -- use --pages deliberately and
only when the budget is there.

Usage:
    PYTHONPATH=src pipenv run python scripts/pull_maryland_leads.py
    PYTHONPATH=src pipenv run python scripts/pull_maryland_leads.py --force
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from skip_tracer.batchdata_client import BatchDataClient
from skip_tracer.database import (
    BATCHDATA_PAGE_SIZE,
    connect,
    count_properties,
    import_search_response,
    init_schema,
    is_in_cooldown,
    record_search_run,
)

load_dotenv()

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "batchdata_search_results"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        default="Maryland",
        help='Free-text geofence. BatchData ignores county/state fields, so this is the '
        'only way to scope results (e.g. "Maryland", "Montgomery County, MD").',
    )
    parser.add_argument(
        "--quicklist",
        default="preforeclosure",
        help="quickLists filter. Defaults to the umbrella foreclosure flag (see module docstring).",
    )
    parser.add_argument("--skip", type=int, default=0, help="Pagination offset.")
    parser.add_argument(
        "--take",
        type=int,
        default=BATCHDATA_PAGE_SIZE,
        help=f"Requested page size (BatchData caps responses at {BATCHDATA_PAGE_SIZE}).",
    )
    parser.add_argument("--cooldown-days", type=int, default=30)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the cooldown and pay for a fresh pull anyway.",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args()

    api_token = os.environ.get("BATCHDATA_API_KEY")
    if not api_token:
        print("BATCHDATA_API_KEY not set — refusing to make a billable call without it.")
        raise SystemExit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with connect() as conn:
        init_schema(conn)

        in_cooldown, last_run_at = is_in_cooldown(
            conn, args.query, args.quicklist, args.cooldown_days
        )
        if in_cooldown and not args.force:
            print(
                f"Last successful pull for ({args.query!r}, {args.quicklist!r}) was "
                f"{last_run_at:%Y-%m-%d %H:%M UTC} — inside the {args.cooldown_days}-day "
                "cooldown. Query the database instead of paying again, or pass --force."
            )
            raise SystemExit(1)

        before = count_properties(conn)
        search_criteria = {"query": args.query, "quickLists": [args.quicklist]}

        print(f"Calling BatchData property/search: {search_criteria!r} skip={args.skip}")
        print("This is a billable call against a live, funded BatchData account.")

        client = BatchDataClient(api_token)
        response, error, error_body = None, None, None
        try:
            response = client.search_properties(
                search_criteria, skip=args.skip, take=args.take
            )
        except Exception as exc:  # noqa: BLE001 - never lose a paid call to a crash
            error = str(exc)
            http_response = getattr(exc, "response", None)
            if http_response is not None:
                try:
                    error_body = http_response.json()
                except ValueError:
                    error_body = http_response.text
            print(f"Call failed: {error}")
            if error_body:
                print(f"Response body: {error_body}")

        # Save raw to disk before touching the database — belt and suspenders.
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^A-Za-z0-9]+", "_", args.query).strip("_")
        out_path = out_dir / f"search_{slug}_{args.quicklist}_{timestamp}.json"
        out_path.write_text(
            json.dumps(
                {
                    "request": {
                        "search_criteria": search_criteria,
                        "skip": args.skip,
                        "take": args.take,
                    },
                    "response": response,
                    "error": error,
                    "error_body": error_body,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Saved raw response to {out_path}")

        if response is None:
            record_search_run(
                conn, args.query, args.quicklist, args.skip, args.take, None, error
            )
            print("Recorded the failed call for auditing. Nothing imported.")
            raise SystemExit(1)

        run_id, ids = import_search_response(
            conn,
            response,
            query=args.query,
            quicklist=args.quicklist,
            skip=args.skip,
            take=args.take,
        )
        after = count_properties(conn)

        meta = ((response.get("results") or {}).get("meta") or {}).get("results", {})
        print(f"\nSearch run #{run_id}")
        print(f"  Server-side matches available : {meta.get('resultsFound')}")
        print(f"  Properties returned this call : {len(ids)}")
        print(f"  New properties added          : {after - before}")
        print(f"  Total properties in catalog   : {after}")

        remaining = (meta.get("resultsFound") or 0) - args.skip - len(ids)
        if remaining > 0:
            pages = -(-remaining // BATCHDATA_PAGE_SIZE)
            print(
                f"\n{remaining} more match(es) exist. Fetching them all would take "
                f"~{pages} more billable call(s) (--skip {args.skip + len(ids)} next)."
            )


if __name__ == "__main__":
    main()
