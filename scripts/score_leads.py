"""Score every catalogued property into the `leads` table.

Free to run — reads only from Postgres, never calls BatchData. Run it
after a monthly pull, or any time the scoring weights in
skip_tracer.lead_scoring change, to re-rank the call list.

Re-scoring never destroys call history: upsert_lead() deliberately leaves
status, notes, phone, email and last_contacted_at untouched on conflict.

Usage:
    PYTHONPATH=src pipenv run python scripts/score_leads.py
    PYTHONPATH=src pipenv run python scripts/score_leads.py --state MD --top 25
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from skip_tracer.database import connect, init_schema, list_properties, upsert_lead
from skip_tracer.lead_scoring import score_properties

load_dotenv()


def _currency(value) -> str:
    return f"${float(value):,.0f}" if value is not None else "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=None, help="Limit to one state, e.g. MD")
    parser.add_argument("--top", type=int, default=25, help="How many to print")
    args = parser.parse_args()

    with connect() as conn:
        init_schema(conn)
        properties = list_properties(conn, state=args.state)
        if not properties:
            print("No properties in the catalog yet — run scripts/pull_maryland_leads.py first.")
            return

        scored = score_properties(properties)
        by_id = {prop["batchdata_id"]: prop for prop in properties}

        qualified = 0
        for batchdata_id, score in scored:
            upsert_lead(conn, batchdata_id, score)
            if not score["disqualified"]:
                qualified += 1

        print(
            f"Scored {len(scored)} property(ies)"
            f"{f' in {args.state}' if args.state else ''}: "
            f"{qualified} qualified, {len(scored) - qualified} disqualified.\n"
        )

        print(f"Top {min(args.top, qualified)} by call priority:")
        shown = 0
        for batchdata_id, score in scored:
            if score["disqualified"] or shown >= args.top:
                continue
            shown += 1
            prop = by_id[batchdata_id]
            print(
                f"{shown:>3}. [{score['total_score']:>6.1f}] "
                f"{prop['street']}, {prop['city']} {prop['state']} {prop['zip']}"
            )
            print(
                f"      profit {_currency(score['estimated_profit']):>12} | "
                f"motivation {score['motivation_score']:>5.0f} | "
                f"profit-score {score['profit_score']:>5.0f} | "
                f"contact {score['contactability_score']:>5.0f}"
            )
            print(f"      owner: {prop['owner_name'] or 'unknown'}")


if __name__ == "__main__":
    main()
