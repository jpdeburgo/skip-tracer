"""CRUD for the leads table -- record what happened on each call.

This is the feedback loop. The scoring weights in skip_tracer.lead_scoring
are a reasoned hypothesis, not a model fitted to conversion data; the only
way they ever become empirical is if call outcomes get written down here.

Usage:
    PYTHONPATH=src pipenv run python scripts/manage_leads.py list --limit 10
    PYTHONPATH=src pipenv run python scripts/manage_leads.py show 12
    PYTHONPATH=src pipenv run python scripts/manage_leads.py status 12 called --contacted \\
        --notes "Left voicemail, callback Tue"
    PYTHONPATH=src pipenv run python scripts/manage_leads.py contact 12 --phone 240-555-0134
    PYTHONPATH=src pipenv run python scripts/manage_leads.py contact 12 --do-not-call
    PYTHONPATH=src pipenv run python scripts/manage_leads.py stats
    PYTHONPATH=src pipenv run python scripts/manage_leads.py delete 12
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from skip_tracer.database import (
    VALID_LEAD_STATUSES,
    connect,
    delete_lead,
    get_lead,
    get_property,
    init_schema,
    set_lead_contact_info,
    top_maryland_leads,
    update_lead_status,
)

load_dotenv()


def _money(value) -> str:
    return f"${float(value):,.0f}" if value is not None else "unknown"


def cmd_list(conn, args) -> int:
    rows = top_maryland_leads(conn, limit=args.limit)
    if not rows:
        print("No qualified Maryland leads — run scripts/score_leads.py first.")
        return 0
    rows = [r for r in rows if args.status is None or r["status"] == args.status]
    print(f"{'id':>5}  {'score':>6}  {'status':<14} {'spread':>12}  address")
    for row in rows:
        address = ", ".join(p for p in (row["street"], row["city"]) if p)
        print(
            f"{row['id']:>5}  {float(row['total_score'] or 0):>6.1f}  "
            f"{row['status']:<14} {_money(row['estimated_profit']):>12}  {address}"
        )
    return 0


def cmd_show(conn, args) -> int:
    lead = get_lead(conn, args.lead_id)
    if lead is None:
        print(f"No lead with id {args.lead_id}.", file=sys.stderr)
        return 1
    prop = get_property(conn, lead["batchdata_id"]) or {}
    address = ", ".join(
        p for p in (prop.get("street"), prop.get("city"), prop.get("state"), prop.get("zip")) if p
    )
    print(f"Lead {lead['id']} — {address}")
    print(f"  Owner:        {prop.get('owner_name') or 'unknown'}")
    print(f"  Status:       {lead['status']}")
    print(f"  Phone:        {lead['phone'] or 'not skip-traced'}")
    print(f"  Email:        {lead['email'] or '-'}")
    print(f"  Do not call:  {lead['do_not_call']}")
    print(f"  Last contact: {lead['last_contacted_at'] or 'never'}")
    print(f"  Value:        {_money(prop.get('estimated_value'))}")
    print(f"  Owed:         {_money(prop.get('total_open_lien_balance'))}")
    print(f"  Spread:       {_money(lead['estimated_profit'])}")
    print(f"  Total score:  {float(lead['total_score'] or 0):.1f}")
    print(
        f"    motivation {float(lead['motivation_score'] or 0):.0f} | "
        f"profit {float(lead['profit_score'] or 0):.0f} | "
        f"contactability {float(lead['contactability_score'] or 0):.0f}"
    )
    print(f"  Signals:      {', '.join(prop.get('quick_lists') or []) or 'none'}")
    if lead["disqualified"]:
        print(f"  DISQUALIFIED: {lead['disqualified_reason']}")
    if lead["notes"]:
        print(f"  Notes:        {lead['notes']}")
    return 0


def cmd_status(conn, args) -> int:
    try:
        updated = update_lead_status(
            conn, args.lead_id, args.status, notes=args.notes, mark_contacted=args.contacted
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not updated:
        print(f"No lead with id {args.lead_id}.", file=sys.stderr)
        return 1
    print(f"Lead {args.lead_id} -> {args.status}.")
    return 0


def cmd_contact(conn, args) -> int:
    if args.phone is None and args.email is None and not args.do_not_call:
        print("Nothing to set: pass --phone, --email, or --do-not-call.", file=sys.stderr)
        return 2
    updated = set_lead_contact_info(
        conn,
        args.lead_id,
        phone=args.phone,
        email=args.email,
        do_not_call=True if args.do_not_call else None,
    )
    if not updated:
        print(f"No lead with id {args.lead_id}.", file=sys.stderr)
        return 1
    print(f"Updated contact info for lead {args.lead_id}.")
    return 0


def cmd_delete(conn, args) -> int:
    if delete_lead(conn, args.lead_id):
        print(f"Deleted lead {args.lead_id}.")
        return 0
    print(f"No lead with id {args.lead_id}.", file=sys.stderr)
    return 1


def cmd_stats(conn, args) -> int:
    """Conversion funnel -- the data that will eventually validate the weights."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status,
                   COUNT(*)              AS n,
                   AVG(total_score)      AS avg_score,
                   AVG(motivation_score) AS avg_motivation
            FROM leads
            WHERE disqualified = FALSE
            GROUP BY status
            ORDER BY n DESC
            """
        )
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS n FROM leads WHERE disqualified")
        disqualified = cur.fetchone()["n"]

    if not rows:
        print("No leads scored yet — run scripts/score_leads.py first.")
        return 0

    print(f"{'status':<16} {'count':>6} {'avg score':>10} {'avg motivation':>15}")
    for row in rows:
        print(
            f"{row['status']:<16} {row['n']:>6} "
            f"{float(row['avg_score'] or 0):>10.1f} {float(row['avg_motivation'] or 0):>15.1f}"
        )
    print(f"\n{disqualified} disqualified lead(s) excluded from the call list.")

    worked = sum(r["n"] for r in rows if r["status"] in {"appointment", "offer_made", "under_contract"})
    called = sum(r["n"] for r in rows if r["status"] != "new")
    if called:
        print(f"Calls placed: {called} | reached a real conversation: {worked} "
              f"({worked / called * 100:.0f}%)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Top Maryland leads by score")
    p_list.add_argument("--limit", type=int, default=25)
    p_list.add_argument("--status", choices=sorted(VALID_LEAD_STATUSES))
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Full detail for one lead")
    p_show.add_argument("lead_id", type=int)
    p_show.set_defaults(func=cmd_show)

    p_status = sub.add_parser("status", help="Record a call outcome")
    p_status.add_argument("lead_id", type=int)
    p_status.add_argument("status", choices=sorted(VALID_LEAD_STATUSES))
    p_status.add_argument("--notes")
    p_status.add_argument(
        "--contacted", action="store_true", help="Stamp last_contacted_at with now()"
    )
    p_status.set_defaults(func=cmd_status)

    p_contact = sub.add_parser("contact", help="Attach skip-trace results")
    p_contact.add_argument("lead_id", type=int)
    p_contact.add_argument("--phone")
    p_contact.add_argument("--email")
    p_contact.add_argument("--do-not-call", action="store_true")
    p_contact.set_defaults(func=cmd_contact)

    p_delete = sub.add_parser("delete", help="Remove a lead (keeps the property record)")
    p_delete.add_argument("lead_id", type=int)
    p_delete.set_defaults(func=cmd_delete)

    p_stats = sub.add_parser("stats", help="Conversion funnel by status")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    with connect() as conn:
        init_schema(conn)
        return args.func(conn, args)


if __name__ == "__main__":
    raise SystemExit(main())
