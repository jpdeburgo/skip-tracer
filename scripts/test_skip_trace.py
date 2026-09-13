"""Manual verification script — Test-First Checklist item 1.

Run against a real, already-known lead and inspect the actual response
shape before trusting batchdata_client.BatchDataClient.skip_trace() in the
pipeline. Needs BATCHDATA_API_KEY set and the $50 wallet minimum funded.

Usage:
    PYTHONPATH=src pipenv run python scripts/test_skip_trace.py \\
        "7924 Lakenheath Way" Potomac MD 20854
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

from skip_tracer.batchdata_client import BatchDataClient, PropertyAddress

load_dotenv()


def main() -> None:
    if len(sys.argv) != 5:
        print(f"Usage: {sys.argv[0]} <street> <city> <state> <zip>")
        raise SystemExit(1)

    api_token = os.environ["BATCHDATA_API_KEY"]
    street, city, state, zipcode = sys.argv[1:5]
    client = BatchDataClient(api_token)
    result = client.skip_trace(PropertyAddress(street, city, state, zipcode))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
