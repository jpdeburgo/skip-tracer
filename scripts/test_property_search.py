"""Manual verification script for BatchDataClient.search_properties().

UNVERIFIED endpoint (see batchdata_client.py's search_properties()
docstring) — this script exists to find the real request/response shape
with the smallest possible number of billable calls, the same way
test_skip_trace.py/test_valuation.py verified skip_trace()/
lookup_valuation() before either was trusted in cli.py.

Every raw response is saved to disk (see --out below) before anything
else happens, so a single paid call is never lost to a crash, a typo in
what you print, or a future need to re-inspect the exact shape — you
should never need to re-run this against the live API to see a response
you already paid for once.

This is NOT wired into cli.py's pipeline and does not run by default —
property/search is a bulk/billable discovery call, unlike the
already-verified per-address skip-trace/valuation calls, so keep --take
small (default 5) until you've confirmed the shape and the per-call cost.

Usage:
    PYTHONPATH=src pipenv run python scripts/test_property_search.py \\
        --county Montgomery --state MD --take 5
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from skip_tracer.batchdata_client import BatchDataClient

load_dotenv()

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "batchdata_search_results"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manually verify BatchDataClient.search_properties() against a live account"
    )
    parser.add_argument("--county", required=True, help="e.g. Montgomery")
    parser.add_argument("--state", default="MD")
    parser.add_argument(
        "--quicklist",
        default="preforeclosure",
        help="quickLists boolean to filter on (e.g. preforeclosure, noticeOfDefault, inherited)",
    )
    parser.add_argument(
        "--take",
        type=int,
        default=5,
        help="Max properties to request (keep small — this is a billable bulk call, cost per "
        "call is not yet confirmed)",
    )
    parser.add_argument("--skip", type=int, default=0)
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

    search_criteria = {
        "county": args.county,
        "state": args.state,
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
        # body explaining *why* it's a 400) on .response — capture that too,
        # since the exception string alone only has the HTTP status line.
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                error_body = response.json()
            except ValueError:
                error_body = response.text
        print(f"Call failed: {error}")
        if error_body:
            print(f"Response body: {error_body}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"search_{args.county}_{args.quicklist}_{timestamp}.json"
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


if __name__ == "__main__":
    main()
