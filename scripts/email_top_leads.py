"""Email the top Maryland leads, with per-lead call prep, as an HTML digest.

Reads only from Postgres, so it is free to run as often as you like and
can go on a cron independent of the (billable) monthly BatchData pull.

Each lead is converted into a cli.Lead so the digest reuses
cli.call_script() -- the PCMT call-prep logic derived from the meeting
transcript -- rather than reimplementing a second, divergent script.

Usage:
    PYTHONPATH=src pipenv run python scripts/email_top_leads.py --dry-run
    PYTHONPATH=src pipenv run python scripts/email_top_leads.py --to me@example.com
"""

from __future__ import annotations

import argparse
import html
import os

from dotenv import load_dotenv

from skip_tracer.cli import Lead, call_script
from skip_tracer.database import connect, init_schema, top_maryland_leads

load_dotenv()

# BatchData quickList flags -> the human-readable signal names call_script()
# matches on. Order matters: call_script picks the FIRST match, so the most
# urgent / most specific angle has to come first.
_FLAG_LABELS: list[tuple[str, str]] = [
    ("noticeOfSale", "Notice of Sale"),
    ("noticeOfDefault", "Notice of Default"),
    ("noticeOfLisPendens", "Notice of Lis Pendens"),
    ("activeAuction", "Active Auction"),
    ("preforeclosure", "Pre-Foreclosure"),
    ("taxDefault", "Tax Default"),
    ("inherited", "Inherited"),
    ("tiredLandlord", "Tired Landlord"),
    ("vacant", "Vacant"),
    ("absenteeOwner", "Absentee Owner"),
    ("freeAndClear", "Free and Clear"),
    ("highEquity", "High Equity"),
    ("failedListing", "Failed Listing"),
    ("expiredListing", "Expired Listing"),
]


def _distress_flags(quick_lists: list[str]) -> list[str]:
    present = set(quick_lists or [])
    return [label for flag, label in _FLAG_LABELS if flag in present]


def _money(value) -> str:
    return f"${float(value):,.0f}" if value is not None else "unknown"


def row_to_lead(row: dict) -> Lead:
    flags = _distress_flags(row.get("quick_lists") or [])
    address = ", ".join(
        part for part in (row.get("street"), row.get("city"), row.get("zip")) if part
    )
    return Lead(
        acctid=row["batchdata_id"],
        address=address or "unknown address",
        owner_name=row.get("owner_name") or "unknown",
        phone=row.get("phone"),
        do_not_call=row.get("do_not_call"),
        motivation_signal=flags[0] if flags else "Absentee owner",
        distress_flags=flags,
        assessed_value=(
            float(row["estimated_value"]) if row.get("estimated_value") is not None else None
        ),
        total_lien_balance=(
            float(row["total_open_lien_balance"])
            if row.get("total_open_lien_balance") is not None
            else None
        ),
        payoff_profit=(
            float(row["estimated_profit"])
            if row.get("estimated_profit") is not None
            else None
        ),
        in_preforeclosure="preforeclosure" in (row.get("quick_lists") or []),
        high_priority=float(row.get("total_score") or 0) >= 70,
    )


def build_html(rows: list[dict]) -> str:
    parts = [
        "<html><body style=\"font-family:-apple-system,Segoe UI,Helvetica,sans-serif;"
        "font-size:14px;color:#111\">",
        f"<h2>Top {len(rows)} Maryland leads by call priority</h2>",
        "<p style=\"color:#555\">Ranked by a blended motivation / profit / contactability "
        "score. Motivation is weighted highest on purpose: profit is what a deal is worth "
        "<em>if</em> it closes, motivation is what decides whether it closes at all.</p>",
    ]

    for index, row in enumerate(rows, start=1):
        lead = row_to_lead(row)
        score = float(row.get("total_score") or 0)
        badge = "#b00020" if score >= 70 else "#8a6d00"

        parts.append("<hr style='border:none;border-top:1px solid #ddd;margin:18px 0'>")
        parts.append(
            f"<h3 style='margin:0 0 4px'>{index}. {html.escape(lead.address)} "
            f"<span style='color:{badge}'>[{score:.0f}]</span></h3>"
        )
        dnc = (
            " <b style='color:#b00020'>DO NOT CALL</b>" if row.get("do_not_call") else ""
        )
        parts.append(
            f"<div style='color:#333'><b>Owner:</b> {html.escape(lead.owner_name)}"
            f" &nbsp;|&nbsp; <b>Phone:</b> {html.escape(lead.phone or 'not yet skip-traced')}"
            f"{dnc}</div>"
        )
        equity = (
            f"{float(row['equity_percent']):.0f}%"
            if row.get("equity_percent") is not None
            else "unknown"
        )
        parts.append(
            f"<div style='color:#333'><b>Est. value:</b> {_money(row.get('estimated_value'))}"
            f" &nbsp;|&nbsp; <b>Owed:</b> {_money(row.get('total_open_lien_balance'))}"
            f" &nbsp;|&nbsp; <b>Spread:</b> {_money(row.get('estimated_profit'))}"
            f" &nbsp;|&nbsp; <b>Equity:</b> {equity}</div>"
        )
        parts.append(
            f"<div style='color:#555'><b>Signals:</b> "
            f"{html.escape(', '.join(lead.distress_flags) or 'none')}</div>"
        )
        parts.append(
            "<div style='color:#555;font-size:12px'>"
            f"motivation {float(row.get('motivation_score') or 0):.0f} &middot; "
            f"profit {float(row.get('profit_score') or 0):.0f} &middot; "
            f"contactability {float(row.get('contactability_score') or 0):.0f}</div>"
        )
        parts.append("<div style='margin-top:8px'><b>Call prep</b><ul style='margin:4px 0'>")
        for line in call_script(lead):
            parts.append(f"<li>{html.escape(line)}</li>")
        parts.append("</ul></div>")

    parts.append(
        "<hr><p style='color:#777;font-size:12px'>Scores are a reasoned model, not one "
        "fitted to your conversion history. Record every call outcome with "
        "scripts/manage_leads.py so the weights can eventually be validated against "
        "what actually closes.</p></body></html>"
    )
    return "".join(parts)


def build_text(rows: list[dict]) -> str:
    lines = [f"Top {len(rows)} Maryland leads by call priority", ""]
    for index, row in enumerate(rows, start=1):
        lead = row_to_lead(row)
        lines.append(f"{index}. [{float(row.get('total_score') or 0):.0f}] {lead.address}")
        lines.append(f"    Owner: {lead.owner_name} | Phone: {lead.phone or 'not skip-traced'}")
        lines.append(
            f"    Value {_money(row.get('estimated_value'))} | "
            f"Owed {_money(row.get('total_open_lien_balance'))} | "
            f"Spread {_money(row.get('estimated_profit'))}"
        )
        lines.append(f"    Signals: {', '.join(lead.distress_flags) or 'none'}")
        for line in call_script(lead):
            lines.append(f"      - {line}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--to", default=os.environ.get("DIGEST_EMAIL_TO"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest instead of sending it (no Gmail auth needed).",
    )
    args = parser.parse_args()

    with connect() as conn:
        init_schema(conn)
        rows = top_maryland_leads(conn, limit=args.limit)

    if not rows:
        print("No qualified Maryland leads — run scripts/score_leads.py first.")
        return

    if args.dry_run:
        print(build_text(rows))
        return

    if not args.to:
        parser.error("no recipient: pass --to or set DIGEST_EMAIL_TO in .env")

    # Imported lazily so --dry-run works in environments without Gmail creds.
    from skip_tracer.gmail_client import get_gmail_service, send_email

    service = get_gmail_service()
    message_id = send_email(
        service,
        subject=f"Top {len(rows)} Maryland leads to call",
        body_text=build_html(rows),
        recipient=args.to,
        html=True,
    )
    print(f"Sent {len(rows)} leads to {args.to} (message {message_id}).")


if __name__ == "__main__":
    main()
