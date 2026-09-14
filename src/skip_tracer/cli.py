"""Weekly MD off-market lead pipeline: gather -> filter -> enrich -> digest.

Each stage is independently testable, matching the nw-deal-screener CLI
pattern: gather_new_leads() -> enrich_lead() -> build_digest_body() ->
send_weekly_digest().

Skip-trace/valuation response parsing in enrich_lead() is verified against
a real lead (see batchdata_client.py's module docstring) but still wrapped
defensively — a failed or unexpected-shaped BatchData call degrades that
one lead's enrichment instead of failing the whole run.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

from .batchdata_client import (
    BatchDataClient,
    PropertyAddress,
    flag_low_margin,
    max_allowable_offer,
)
from .condition import condition_tier, estimate_repair_cost
from .filtering import classify_owner_entity, contactability_tier, is_genuinely_absentee
from .gmail_client import get_gmail_service, send_email
from .imap_client import PROCESSING_ORDER, fetch_jurisdiction_leads
from .state import load_seen_parcels, save_seen_parcels
from .zillow import zillow_search_link

load_dotenv()

# BatchData has no free trial and bills per skip-trace/valuation/permit call
# — cap spend per run the same way nw-deal-screener day-gates RentCast.
DEFAULT_MAX_LEADS_PER_RUN = 25

# CONVEY1 code 4 with a null CONSIDR1 (consideration) marks a transfer with
# no sale price recorded — typically inheritance, divorce, or a similar
# non-arms-length transfer, a strong motivation signal. Confirmed live that
# MD iMap returns CONVEY1 as an int, not a string.
NON_SALE_TRANSFER_CODE = 4

# BatchData's valuation lookup returns a quickLists object of dozens of
# booleans — this is the subset that's actually decision-relevant for an
# off-market flip lead. Order matters: it's the display order in the digest.
DISTRESS_FLAG_LABELS = {
    "vacant": "Vacant",
    "preforeclosure": "Pre-Foreclosure",
    "noticeOfDefault": "Notice of Default",
    "noticeOfSale": "Notice of Sale",
    "noticeOfLisPendens": "Notice of Lis Pendens",
    "taxDefault": "Tax Default",
    "involuntaryLien": "Involuntary Lien",
    "inherited": "Inherited",
    "tiredLandlord": "Tired Landlord",
    "freeAndClear": "Free and Clear (no mortgage)",
    "highEquity": "High Equity",
    "lowEquity": "Low Equity",
}


def _distress_flags(quick_lists: dict[str, Any]) -> list[str]:
    return [label for key, label in DISTRESS_FLAG_LABELS.items() if quick_lists.get(key)]


@dataclass
class Lead:
    acctid: str
    address: str
    owner_name: str = "unknown"
    phone: str | None = None
    do_not_call: bool | None = None
    tcpa_risk: bool | None = None
    motivation_signal: str = "Absentee owner"
    contactability_tier: str = "direct"
    entity_type: str = "individual"
    condition_tier: str = "unassessed"
    assessed_value: float | None = None
    arv_estimate: float | None = None
    repair_cost_estimate: float | None = None
    mao_estimate: float | None = None
    low_margin: bool | None = None
    distress_flags: list[str] = field(default_factory=list)
    zillow_link: str = ""


def _motivation_signal(record: dict[str, Any]) -> str:
    if (
        record.get("CONVEY1") == NON_SALE_TRANSFER_CODE
        and not record.get("CONSIDR1")
    ):
        return "Non-sale transfer (possible inheritance)"
    return "Absentee owner"


def gather_new_leads(
    seen: set[str], jurisdictions: list[str] | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """Fetch, dedupe, and filter candidate records — no BatchData calls yet.

    Stops as soon as `limit` qualifying candidates are found. A single
    county can return tens of thousands of genuinely-absentee records
    (confirmed live: ~38k for Montgomery alone), and classify_owner_entity()
    costs ~10ms/record once its NER model is warm — scanning every record
    in every jurisdiction before capping to what a run will actually enrich
    would make gather_new_leads() the slow part of the pipeline for no
    reason. Leads past the limit are simply left unseen and picked up
    naturally on a future run, once already-enriched ACCTIDs are in `seen`.
    """
    new_records = []
    for jurs_code in jurisdictions or PROCESSING_ORDER:
        for record in fetch_jurisdiction_leads(jurs_code):
            if limit is not None and len(new_records) >= limit:
                return new_records

            acctid = record.get("ACCTID")
            if not acctid or acctid in seen:
                continue
            if not is_genuinely_absentee(record):
                continue

            entity_type = classify_owner_entity(record)
            if entity_type == "diplomatic":
                continue

            record["entity_type"] = entity_type
            record["contactability_tier"] = contactability_tier(record)
            new_records.append(record)
    return new_records


def _year_from_iso_date(value: str | None) -> int | None:
    try:
        return int(value[:4])
    except (TypeError, ValueError):
        return None


def enrich_lead(record: dict[str, Any], batchdata: BatchDataClient | None) -> Lead:
    """Adds skip-trace contact info, valuation, and a condition tier to an
    already-filtered MD iMap record. Degrades gracefully when BatchData is
    unavailable or a call fails.

    Two billable calls per lead (skip-trace + valuation), not five: the
    valuation lookup's embedded permit summary covers condition_tier()'s
    needs, and skip-trace's embedded per-phone "dnc" flag and person-level
    TCPA flag cover this digest's compliance signal — see
    batchdata_client.py's module docstring for why the other three
    endpoints aren't called here.
    """
    lead = Lead(
        acctid=record["ACCTID"],
        address=record.get("ADDRESS", ""),
        motivation_signal=_motivation_signal(record),
        contactability_tier=record.get("contactability_tier", "direct"),
        entity_type=record.get("entity_type", "individual"),
        assessed_value=record.get("NFMTTLVL"),
        zillow_link=zillow_search_link(
            street=record.get("ADDRESS", ""),
            premise_city=record.get("PREMCITY", ""),
            state="MD",
            zipcode=record.get("PREMZIP", ""),
        ),
    )

    permit_history: list[dict[str, Any]] = []
    if batchdata is not None:
        address = PropertyAddress(
            street=record.get("ADDRESS", ""),
            city=record.get("PREMCITY", ""),
            state="MD",
            zipcode=record.get("PREMZIP", ""),
        )
        try:
            skip_trace_result = batchdata.skip_trace(address)
            person = (skip_trace_result.get("results") or {}).get("persons", [{}])[0]
            phones = person.get("phoneNumbers", [])
            if phones:
                lead.phone = phones[0].get("number")
                lead.do_not_call = phones[0].get("dnc")
            lead.tcpa_risk = person.get("dnc", {}).get("tcpa")
            # The deed owner, not necessarily the person tied to the phone
            # number above (that's whoever's reachable at the owner's
            # mailing address, per BatchData) — see batchdata_client.py's
            # module docstring.
            deed_owner_name = (
                person.get("property", {}).get("owner", {}).get("name", {}).get("full")
            )
            if deed_owner_name:
                lead.owner_name = deed_owner_name
        except Exception as error:  # noqa: BLE001 - degrade, don't fail the run
            print(f"skip-trace failed for {lead.acctid}: {error}")

        try:
            valuation_result = batchdata.lookup_valuation(address)
            properties = (valuation_result.get("results") or {}).get("properties", [])
            if properties:
                prop = properties[0]
                lead.arv_estimate = prop.get("valuation", {}).get("estimatedValue")
                owner_full_name = prop.get("owner", {}).get("fullName")
                if owner_full_name:
                    lead.owner_name = owner_full_name
                permit = prop.get("permit") or {}
                latest_permit_year = _year_from_iso_date(permit.get("latestDate"))
                if permit.get("permitCount") and latest_permit_year is not None:
                    permit_history = [{"year": latest_permit_year}]
                lead.distress_flags = _distress_flags(prop.get("quickLists") or {})
        except Exception as error:  # noqa: BLE001
            print(f"valuation lookup failed for {lead.acctid}: {error}")

        if lead.assessed_value is not None and lead.arv_estimate is not None:
            lead.low_margin = flag_low_margin(lead.assessed_value, lead.arv_estimate)

    lead.condition_tier = condition_tier(record, permit_history)
    lead.repair_cost_estimate = estimate_repair_cost(
        lead.condition_tier, record.get("SQFTSTRC")
    )
    if lead.arv_estimate is not None and lead.repair_cost_estimate is not None:
        lead.mao_estimate = max_allowable_offer(
            lead.arv_estimate, lead.repair_cost_estimate
        )
    return lead


def _format_currency(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "unknown"


def build_digest_body(leads: list[Lead]) -> str:
    body_lines = []
    for lead in leads:
        margin_note = ""
        if lead.low_margin is True:
            margin_note = " | LOW MARGIN (current value >= ARV estimate)"
        dnc_note = " | DNC" if lead.do_not_call else ""
        tcpa_note = " | TCPA RISK" if lead.tcpa_risk else ""
        repair_note = (
            " (rule-of-thumb, not a quote)" if lead.repair_cost_estimate is not None else ""
        )
        mao_line = (
            f"  Max allowable offer (70% rule): {_format_currency(lead.mao_estimate)}\n"
            if lead.mao_estimate is not None
            else ""
        )
        distress_line = (
            f"  Distress signals: {', '.join(lead.distress_flags)}\n"
            if lead.distress_flags
            else ""
        )
        body_lines.append(
            f"{lead.address}\n"
            f"  Owner: {lead.owner_name} | Phone: {lead.phone or 'n/a'}{dnc_note}{tcpa_note} | "
            f"Signal: {lead.motivation_signal} | Condition: {lead.condition_tier}"
            f"{margin_note}\n"
            f"{distress_line}"
            f"  Current value: {_format_currency(lead.assessed_value)} | "
            f"Est. repair cost: {_format_currency(lead.repair_cost_estimate)}{repair_note} | "
            f"Potential ARV: {_format_currency(lead.arv_estimate)}\n"
            f"{mao_line}"
            f"  Zillow: {lead.zillow_link}\n"
        )
    return "\n".join(body_lines)


def send_weekly_digest(leads: list[Lead], to_email: str) -> None:
    service = get_gmail_service()
    send_email(
        service,
        subject=f"Weekly MD Off-Market Leads - {len(leads)} new",
        body_text=build_digest_body(leads),
        recipient=to_email,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly MD off-market lead pipeline")
    parser.add_argument(
        "--jurisdictions",
        nargs="+",
        help="Limit to specific jurisdiction codes (default: all, priority order)",
    )
    parser.add_argument("--no-email", action="store_true", help="Skip sending the digest")
    args = parser.parse_args()

    seen = load_seen_parcels()
    max_leads = int(
        os.environ.get("BATCHDATA_MAX_LEADS_PER_RUN", DEFAULT_MAX_LEADS_PER_RUN)
    )
    candidates = gather_new_leads(seen, args.jurisdictions, limit=max_leads)
    print(
        f"Found {len(candidates)} new candidate lead(s) after filtering "
        f"(capped at BATCHDATA_MAX_LEADS_PER_RUN={max_leads}; any more are "
        f"picked up on a future run)."
    )

    api_token = os.environ.get("BATCHDATA_API_KEY")
    batchdata = BatchDataClient(api_token) if api_token else None
    if batchdata is None:
        print("BATCHDATA_API_KEY not set; skipping skip-trace/valuation.")

    leads = [enrich_lead(record, batchdata) for record in candidates]

    if leads and not args.no_email:
        send_weekly_digest(leads, os.environ["DIGEST_EMAIL_TO"])
        print(f"Digest sent for {len(leads)} lead(s).")
    elif leads:
        print(f"{len(leads)} lead(s) ready; --no-email set, skipping send.")
    else:
        print("Nothing new this run.")

    seen.update(record["ACCTID"] for record in candidates)
    save_seen_parcels(seen)


if __name__ == "__main__":
    main()
