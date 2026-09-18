"""Manual verification script for BatchDataClient.search_properties().

VERIFIED against a real, funded account (see batchdata_client.py's
search_properties() docstring for full detail) — this script exists to
keep experimenting with search_criteria filters (quickLists combos,
valuation ranges, etc.) with the smallest possible number of billable
calls, the same way test_skip_trace.py/test_valuation.py were used to
verify skip_trace()/lookup_valuation() before either was trusted in
cli.py.

Every raw response is saved to disk (see --out below) before anything
else happens, so a single paid call is never lost to a crash, a typo in
what you print, or a future need to re-inspect the exact shape — you
should never need to re-run this against the live API to see a response
you already paid for once.

This is NOT wired into cli.py's pipeline and does not run by default —
property/search is a bulk/billable discovery call, unlike the
already-verified per-address skip-trace/valuation calls, so keep --take
small (default 5, capped at 25 by BatchData regardless) until you're
ready to build a real pagination loop.

IMPORTANT: use --query "City, ST" or "County County, ST" to geofence
results — county/state search_criteria fields are silently ignored by
BatchData (confirmed live: returned properties scattered nationwide).

MONTHLY GUARD RAIL: this is a bulk/billable call, so a per-(query,
quicklist) cooldown is enforced by default — running the same search
again within 30 days refuses to make the call and tells you to filter
the already-saved response with rank_properties_by_profit() instead. Use
--force to override (e.g. you know the account's data actually changed,
or you're intentionally paying for a fresh pull).

Usage:
    PYTHONPATH=src pipenv run python scripts/test_property_search.py \\
        --query "Montgomery County, MD" --take 5

    # Rank the properties already paid for by estimated profit, no new call:
    PYTHONPATH=src pipenv run python scripts/test_property_search.py \\
        --query "Maryland" --rank-only \\
        --from-file batchdata_search_results/search_Maryland_preforeclosure_....json
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from skip_tracer.batchdata_client import BatchDataClient, rank_properties_by_profit

load_dotenv()

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "batchdata_search_results"
LAST_RUN_FILE = "_last_run.json"
COOLDOWN_DAYS = 30


def _last_run_key(query: str, quicklist: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{query}_{quicklist}").strip("_").lower()


def _load_last_runs(out_dir: Path) -> dict[str, str]:
    path = out_dir / LAST_RUN_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_last_run(out_dir: Path, key: str, timestamp: str) -> None:
    path = out_dir / LAST_RUN_FILE
    last_runs = _load_last_runs(out_dir)
    last_runs[key] = timestamp
    path.write_text(json.dumps(last_runs, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _check_cooldown(out_dir: Path, key: str, force: bool) -> None:
    last_runs = _load_last_runs(out_dir)
    last_run_at = last_runs.get(key)
    if last_run_at is None:
        return
    last_run_time = datetime.fromisoformat(last_run_at)
    elapsed = datetime.now(timezone.utc) - last_run_time
    if elapsed < timedelta(days=COOLDOWN_DAYS) and not force:
        remaining = timedelta(days=COOLDOWN_DAYS) - elapsed
        print(
            f"This search (key={key!r}) already ran {elapsed.days} day(s) ago "
            f"({last_run_at}) — refusing to pay for another bulk pull for "
            f"{remaining.days} more day(s). Filter the already-saved response(s) "
            f"in {out_dir}/ with rank_properties_by_profit() instead, or pass "
            "--force if you're sure you want to pay for fresh data."
        )
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manually verify BatchDataClient.search_properties() against a live account"
    )
    parser.add_argument(
        "--query",
        required=True,
        help='Free-text geofence, e.g. "Montgomery County, MD", "Rockville, MD", or '
        '"Maryland" for the whole state — the only confirmed way to scope results '
        "to a location.",
    )
    parser.add_argument(
        "--quicklist",
        default="preforeclosure",
        help="quickLists boolean to filter on (e.g. preforeclosure, noticeOfDefault, inherited)",
    )
    parser.add_argument(
        "--take",
        type=int,
        default=5,
        help="Max properties to request (BatchData caps each response at 25 regardless; "
        "use --skip to paginate beyond that)",
    )
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument(
        "--rank-top",
        type=int,
        default=None,
        help="If set, also save a second file with only the top N properties from this "
        "response ranked by estimated payoff profit (valuation minus open liens) — "
        "e.g. --rank-top 25 to pull out the 25 most promising results.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"Bypass the {COOLDOWN_DAYS}-day cooldown for this (query, quicklist) pair.",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        help="Directory to save the raw response JSON to (always saved, regardless of outcome)",
    )
    args = parser.parse_args()

    api_token = os.environ.get("BATCHDATA_API_KEY")
    if not api_token:
        print("BATCHDATA_API_KEY not set — refusing to make a billable call without it.")
        raise SystemExit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cooldown_key = _last_run_key(args.query, args.quicklist)
    _check_cooldown(out_dir, cooldown_key, args.force)

    search_criteria = {
        "query": args.query,
        "quickLists": [args.quicklist],
    }

    print(f"Requesting up to {args.take} result(s), search_criteria={search_criteria!r}")
    print("This is a billable call against a live, funded BatchData account.")

    client = BatchDataClient(api_token)
    error_body = None
    try:
        result = client.search_properties(search_criteria, skip=args.skip, take=args.take)
        error = None
    except Exception as exc:  # noqa: BLE001 - save whatever we can either way
        result = None
        error = str(exc)
        # requests.HTTPError carries the actual server response (often a JSON
        # body explaining *why* it's a 400/403) on .response — capture that
        # too, since the exception string alone only has the HTTP status line.
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                error_body = response.json()
            except ValueError:
                error_body = response.text
        print(f"Call failed: {error}")
        if error_body:
            print(f"Response body: {error_body}")

    # Record this attempt against the cooldown regardless of success/failure
    # — a 403 "insufficient balance" still consumed the request/response
    # cycle and we don't want to hammer the API in a retry loop within the
    # same window; --force always remains available.
    _save_last_run(out_dir, cooldown_key, datetime.now(timezone.utc).isoformat())

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    query_slug = re.sub(r"[^A-Za-z0-9]+", "_", args.query).strip("_")
    out_path = out_dir / f"search_{query_slug}_{args.quicklist}_{timestamp}.json"
    out_path.write_text(
        json.dumps(
            {
                "request": {
                    "search_criteria": search_criteria,
                    "skip": args.skip,
                    "take": args.take,
                },
                "response": result,
                "error": error,
                "error_body": error_body,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Saved raw result (paid for either way) to {out_path}")

    if result is not None:
        print(json.dumps(result, indent=2)[:4000])

        if args.rank_top is not None:
            properties = result.get("results", {}).get("properties", [])
            ranked = rank_properties_by_profit(properties, top_n=args.rank_top)
            ranked_path = (
                out_dir / f"ranked_{query_slug}_{args.quicklist}_top{args.rank_top}_{timestamp}.json"
            )
            ranked_path.write_text(
                json.dumps(ranked, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"Saved top {len(ranked)} by estimated profit to {ranked_path}")
            for prop in ranked:
                address = prop.get("address", {})
                print(
                    f"  {address.get('street')}, {address.get('city')} "
                    f"{address.get('state')} — estimated profit: "
                    f"{prop['_estimated_profit']}"
                )


if __name__ == "__main__":
    main()
