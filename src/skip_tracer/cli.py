"""Weekly MD off-market lead pipeline: gather -> filter -> enrich -> digest.

Each stage is independently testable, matching the nw-deal-screener CLI
pattern: gather_new_leads() -> enrich_lead() -> build_digest_body() ->
send_weekly_digest().

Skip-trace/valuation response parsing in enrich_lead() is UNVERIFIED (see
batchdata_client.py and the Test-First Checklist in README.md) and is
wrapped defensively — a failed or unexpected-shaped BatchData call degrades
that one lead's enrichment instead of failing the whole run.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

from .batchdata_client import (
    BatchDataClient,
    PropertyAddress,
    estimate_arv_from_comps,
    flag_low_margin,
)
from .condition import condition_tier
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


@dataclass
class Lead:
    acctid: str
    address: str
    owner_name: str = "unknown"
    phone: str | None = None
    do_not_call: bool | None = None
    motivation_signal: str = "Absentee owner"
    contactability_tier: str = "direct"
    entity_type: str = "individual"
    condition_tier: str = "unassessed"
    assessed_value: float | None = None
    arv_estimate: float | None = None
    low_margin: bool | None = None
    zillow_link: str = ""


def _motivation_signal(record: dict[str, Any]) -> str:
    if (
        record.get("CONVEY1") == NON_SALE_TRANSFER_CODE
        and not record.get("CONSIDR1")
    ):
        return "Non-sale transfer (possible inheritance)"
    return "Absentee owner"


def gather_new_leads(
    seen: set[str], jurisdictions: list[str] | None = None
) -> list[dict[str, Any]]:
    """Fetch, dedupe, and filter candidate records — no BatchData calls yet."""
    new_records = []
    for jurs_code in jurisdictions or PROCESSING_ORDER:
        for record in fetch_jurisdiction_leads(jurs_code):
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


def enrich_lead(record: dict[str, Any], batchdata: BatchDataClient | None) -> Lead:
    """Adds skip-trace contact info, valuation, permits, and a condition
    tier to an already-filtered MD iMap record. Degrades gracefully when
    BatchData is unavailable or a call fails."""
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
            lead.owner_name = person.get("name", {}).get("full", lead.owner_name)
            phones = person.get("phoneNumbers", [])
            if phones:
                lead.phone = phones[0].get("number")
        except Exception as error:  # noqa: BLE001 - degrade, don't fail the run
            print(f"skip-trace failed for {lead.acctid}: {error}")

        if lead.phone:
            try:
                dnc_result = batchdata.check_dnc(lead.phone)
                lead.do_not_call = bool(
                    (dnc_result.get("results") or {}).get("dnc")
                )
            except Exception as error:  # noqa: BLE001
                print(f"DNC check failed for {lead.acctid}: {error}")

        try:
            valuation_result = batchdata.lookup_valuation(address)
            results = valuation_result.get("results") or {}
            if "arv" in results:
                lead.arv_estimate = results["arv"]
            elif "comps" in results and record.get("SQFTSTRC"):
                lead.arv_estimate = estimate_arv_from_comps(
                    results["comps"], record["SQFTSTRC"]
                )
        except Exception as error:  # noqa: BLE001
            print(f"valuation lookup failed for {lead.acctid}: {error}")

        if lead.assessed_value is not None and lead.arv_estimate is not None:
            lead.low_margin = flag_low_margin(lead.assessed_value, lead.arv_estimate)

        try:
            permit_history = batchdata.get_property_permits(address)
        except Exception as error:  # noqa: BLE001
            print(f"permit lookup failed for {lead.acctid}: {error}")

    lead.condition_tier = condition_tier(record, permit_history)
    return lead


def build_digest_body(leads: list[Lead]) -> str:
    body_lines = []
    for lead in leads:
        margin_note = ""
        if lead.low_margin is True:
            margin_note = " | LOW MARGIN (assessed value >= ARV estimate)"
        dnc_note = " | DNC" if lead.do_not_call else ""
        body_lines.append(
            f"{lead.address}\n"
            f"  Owner: {lead.owner_name} | Phone: {lead.phone or 'n/a'}{dnc_note} | "
            f"Signal: {lead.motivation_signal} | Condition: {lead.condition_tier}"
            f"{margin_note}\n"
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
    candidates = gather_new_leads(seen, args.jurisdictions)
    print(f"Found {len(candidates)} new candidate leads after filtering.")

    max_leads = int(
        os.environ.get("BATCHDATA_MAX_LEADS_PER_RUN", DEFAULT_MAX_LEADS_PER_RUN)
    )
    to_enrich, deferred = candidates[:max_leads], candidates[max_leads:]
    if deferred:
        print(
            f"Deferring BatchData enrichment for {len(deferred)} lead(s) to a "
            f"future run (BATCHDATA_MAX_LEADS_PER_RUN={max_leads})."
        )

    api_token = os.environ.get("BATCHDATA_API_KEY")
    batchdata = BatchDataClient(api_token) if api_token else None
    if batchdata is None:
        print("BATCHDATA_API_KEY not set; skipping skip-trace/valuation/permits.")

    leads = [enrich_lead(record, batchdata) for record in to_enrich]

    if leads and not args.no_email:
        send_weekly_digest(leads, os.environ["DIGEST_EMAIL_TO"])
        print(f"Digest sent for {len(leads)} lead(s).")
    elif leads:
        print(f"{len(leads)} lead(s) ready; --no-email set, skipping send.")
    else:
        print("Nothing new this run.")

    seen.update(record["ACCTID"] for record in to_enrich)
    save_seen_parcels(seen)


if __name__ == "__main__":
    main()
