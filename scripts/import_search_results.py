"""Load already-paid-for BatchData search responses into Render Postgres.

Every property/search response saved under batchdata_search_results/ cost
real money. This backfills them into the database so they're queryable,
scoreable, and permanently safe from a laptop wipe or a stray `rm`.

Idempotent: properties are upserted by their BatchData `_id`, so
re-running this never duplicates a property and never double-counts. Files
that recorded a failed call (no response body) are skipped.

Usage:
    PYTHONPATH=src pipenv run python scripts/import_search_results.py
    PYTHONPATH=src pipenv run python scripts/import_search_results.py --dry-run
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from skip_tracer.database import connect, import_search_response, init_schema

load_dotenv()

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "batchdata_search_results"


def _iter_response_files(directory: Path):
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue  # _last_run.json bookkeeping, not a response
        yield path


def _extract(path: Path) -> tuple[dict | None, dict]:
    """Returns (response, request_metadata) from either wrapper shape.

    Files written by scripts/test_property_search.py wrap the response in
    {"request": ..., "response": ...}; a couple of early ones saved the
    bare response instead, so handle both rather than lose paid data to a
    format mismatch.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if "response" in data:
        return data.get("response"), data.get("request", {})
    return data, {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported without writing to the database",
    )
    args = parser.parse_args()

    directory = Path(args.dir)
    files = list(_iter_response_files(directory))
    if not files:
        print(f"No response files found in {directory}")
        return

    total_properties = 0
    with connect() as conn:
        if not args.dry_run:
            init_schema(conn)

        for path in files:
            response, request = _extract(path)
            properties = ((response or {}).get("results") or {}).get("properties") or []
            if not properties:
                print(f"{path.name}: no properties (failed call) — skipped")
                continue

            criteria = request.get("search_criteria", {})
            query = criteria.get("query") or criteria.get("county") or "unknown"
            quicklists = criteria.get("quickLists") or ["unknown"]
            quicklist = quicklists[0] if isinstance(quicklists, list) else "unknown"

            if args.dry_run:
                print(f"{path.name}: would import {len(properties)} property(ies)")
            else:
                _, ids = import_search_response(
                    conn,
                    response,
                    query=query,
                    quicklist=quicklist,
                    skip=request.get("skip", 0),
                    take=request.get("take", len(properties)),
                )
                print(f"{path.name}: imported {len(ids)} property(ies)")
            total_properties += len(properties)

    verb = "would import" if args.dry_run else "imported"
    print(f"\nDone — {verb} {total_properties} property record(s) from {len(files)} file(s).")


if __name__ == "__main__":
    main()
