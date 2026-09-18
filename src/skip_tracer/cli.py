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
    payoff_profit_estimate,
)
from .archive import (
    load_lead_archive,
    load_qualified_leads,
    record_leads,
    save_lead_archive,
    save_qualified_leads,
)
from .batchdata_cache import cached_call, load_batchdata_cache, save_batchdata_cache
from .condition import condition_tier, estimate_repair_cost
from .filtering import classify_owner_entity, contactability_tier, is_genuinely_absentee
from .gmail_client import get_gmail_service, send_email
from .imap_client import PROCESSING_ORDER, fetch_jurisdiction_leads
from .state import load_seen_parcels, save_seen_parcels
from .zillow import zillow_search_link

load_dotenv()

# BATCHDATA_MAX_LEADS_PER_RUN is a target number of leads worth pursuing to
# find, not a cap on how many candidates get enriched — most candidates get
# filtered out by _exclusion_reasons() below, so enrichment keeps going
# until either that many matches are found or BATCHDATA_MAX_ENRICHMENT_ATTEMPTS
# candidates have been tried, whichever comes first. That second cap is a
# hard ceiling on spend for when the match rate is low; BatchData has no
# free trial and bills per skip-trace/valuation call.
DEFAULT_MAX_LEADS_PER_RUN = 25
DEFAULT_MAX_ENRICHMENT_ATTEMPTS_MULTIPLIER = 5

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

# The subset of DISTRESS_FLAG_LABELS that specifically means an active
# foreclosure filing is on record — the window this pipeline's
# --preforeclosure mode targets, where the owner still legally owns the
# home but is at risk of losing it. taxDefault/involuntaryLien are related
# distress signals but aren't themselves a foreclosure filing, so they stay
# out of this subset.
PREFORECLOSURE_FLAG_KEYS = frozenset(
    {"preforeclosure", "noticeOfDefault", "noticeOfSale", "noticeOfLisPendens"}
)


def _distress_flags(quick_lists: dict[str, Any]) -> list[str]:
    return [label for key, label in DISTRESS_FLAG_LABELS.items() if quick_lists.get(key)]


# Opener angle by distress signal, checked in priority order (most
# time-sensitive/emotionally-specific first) — matches the field lesson
# that "we pay cash" is the wrong opener for probate/inherited sellers
# (who care more about avoiding capital gains and probate hassle than
# cash speed) and that foreclosure/tax-default sellers respond better to
# a timeline-first opener than a generic pitch. Falls through to a
# generic absentee-owner opener when nothing more specific applies.
_INHERITED_OPENER = (
    "Likely inherited/probate property — do NOT open with \"we pay cash.\" "
    "Boomer/inheriting sellers usually care more about avoiding capital "
    "gains and the hassle of probate than about cash speed. Lead with "
    "\"we buy probate/inherited homes and handle the paperwork\" instead."
)
_OPENER_BY_SIGNAL: list[tuple[str, str]] = [
    ("Notice of Sale", "Foreclosure sale is scheduled — lead with the timeline, not price: how much runway do they have left, and would a fast, certain close before the sale date help them avoid a foreclosure on their credit?"),
    ("Notice of Default", "Pre-foreclosure/default notice on file — same urgency angle: focus on avoiding a foreclosure, not on beating another offer."),
    ("Notice of Lis Pendens", "Active lis pendens (litigation) — tread carefully; confirm they're still the decision-maker before pitching anything."),
    ("Pre-Foreclosure", "Pre-foreclosure — lead with timeline/certainty, not price."),
    ("Tax Default", "Tax default on record — ask whether back taxes are current now; a resolved-but-recent default often means a payment plan, POA, or an elderly owner with a caretaker. Don't assume it's fixed for good — repeat delinquency is common."),
    ("Inherited", _INHERITED_OPENER),
    ("Tired Landlord", "Tired-landlord signal — lead with relief from tenant/maintenance hassle, not price."),
    ("Vacant", "Property reads vacant — ask directly what it's costing them to maintain/insure an empty house."),
]


def call_script(lead: Lead) -> list[str]:
    """Per-lead call-prep talking points generated from this lead's own
    attributes (distress_flags, motivation_signal, condition_tier, DNC/TCPA
    flags) — not a static script. Always built around Price / Condition /
    Motivation / Time (PCMT): get those four answers and the call is done,
    regardless of exact wording. Never anchor with our own number first —
    ask the seller what they need to walk away with. A seller with nowhere
    to go after closing isn't a workable deal yet, no matter the margin —
    that's a hang-up-the-phone disqualifier, not just a note.
    """
    opener = "General absentee-owner opener — no specific distress signal on file; keep it open-ended."
    for signal_key, angle in _OPENER_BY_SIGNAL:
        if signal_key in lead.distress_flags:
            opener = angle
            break
    else:
        if "Non-sale transfer" in lead.motivation_signal:
            opener = _INHERITED_OPENER

    lines = [
        f"Opener: {opener}",
        "Price: Ask what number they have in mind / what they need to walk away with — never state our number first.",
        f"Condition: Confirm condition matches our {lead.condition_tier} read; ask what repairs they know about.",
        "Motivation: Ask directly why they're selling now — ties back to the opener above.",
        "Time: Ask how soon they need to close or move, then stress-test it — \"if we could close as soon as tomorrow, would that work?\"",
        "Dealbreaker check: Confirm they have somewhere to go after closing — no exit plan means this isn't a workable deal yet.",
    ]
    if lead.do_not_call or lead.tcpa_risk:
        lines.append("Compliance: DNC/TCPA flagged on this number — call only, do not text.")
    else:
        lines.append("Compliance: No consent on file for texting — call first; only text once they've consented.")
    return lines


def _in_preforeclosure(quick_lists: dict[str, Any]) -> bool:
    return any(quick_lists.get(key) for key in PREFORECLOSURE_FLAG_KEYS)


@dataclass
class Lead:
    acctid: str
    address: str
    owner_name: str = "unknown"
    phone: str | None = None
    email: str | None = None
    do_not_call: bool | None = None
    tcpa_risk: bool | None = None
    motivation_signal: str = "Absentee owner"
    contactability_tier: str = "direct"
    entity_type: str = "individual"
    condition_tier: str = "unassessed"
    assessed_value: float | None = None
    last_sale_price: float | None = None
    last_sale_date: str | None = None
    arv_estimate: float | None = None
    repair_cost_estimate: float | None = None
    mao_estimate: float | None = None
    total_lien_balance: float | None = None
    payoff_profit: float | None = None
    high_priority: bool = False
    in_preforeclosure: bool = False
    low_margin: bool | None = None
    corporate_or_trust_owned: bool = False
    distress_flags: list[str] = field(default_factory=list)
    zillow_link: str = ""


def _motivation_signal(record: dict[str, Any]) -> str:
    if (
        record.get("CONVEY1") == NON_SALE_TRANSFER_CODE
        and not record.get("CONSIDR1")
    ):
        return "Non-sale transfer (possible inheritance)"
    if record.get("is_absentee") is False:
        return "Owner-occupied (pre-foreclosure candidate)"
    return "Absentee owner"


def gather_new_leads(
    seen: set[str],
    jurisdictions: list[str] | None = None,
    limit: int | None = None,
    require_absentee: bool = True,
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

    `require_absentee=False` is the --preforeclosure mode: MD iMap is
    queried with owner-occupied parcels included (a homeowner behind on
    their own mortgage is still living there), and the absentee-address
    filter is skipped rather than used to drop candidates. Unlike absentee
    status, pre-foreclosure status isn't knowable from MD iMap's free data
    at all — only BatchData's quickLists tell you that, in enrich_lead()
    below — so this mode can't pre-filter for it the same way and will bill
    BatchData for candidates that turn out not to be in foreclosure.
    """
    new_records = []
    for jurs_code in jurisdictions or PROCESSING_ORDER:
        for record in fetch_jurisdiction_leads(
            jurs_code, include_owner_occupied=not require_absentee
        ):
            if limit is not None and len(new_records) >= limit:
                return new_records

            acctid = record.get("ACCTID")
            if not acctid or acctid in seen:
                continue

            is_absentee = is_genuinely_absentee(record)
            if require_absentee and not is_absentee:
                continue

            entity_type = classify_owner_entity(record)
            if entity_type == "diplomatic":
                continue

            record["is_absentee"] = is_absentee
            record["entity_type"] = entity_type
            record["contactability_tier"] = contactability_tier(record)
            new_records.append(record)
    return new_records


def _year_from_iso_date(value: str | None) -> int | None:
    try:
        return int(value[:4])
    except (TypeError, ValueError):
        return None


def enrich_lead(
    record: dict[str, Any],
    batchdata: BatchDataClient | None,
    cache: dict[str, Any] | None = None,
) -> Lead:
    """Adds skip-trace contact info, valuation, and a condition tier to an
    already-filtered MD iMap record. Degrades gracefully when BatchData is
    unavailable or a call fails.

    Two billable calls per lead (skip-trace + valuation), not five: the
    valuation lookup's embedded permit summary covers condition_tier()'s
    needs, and skip-trace's embedded per-phone "dnc" flag and person-level
    TCPA flag cover this digest's compliance signal — see
    batchdata_client.py's module docstring for why the other three
    endpoints aren't called here.

    `cache`, if given, is a batchdata_cache.py dict: a call already cached
    for this record's ACCTID is reused instead of re-billing BatchData, and
    `record` itself is stashed in the cache entry so a cached lead can be
    fully replayed later (see scripts/reprocess_from_cache.py) without
    needing MD iMap either.
    """
    if cache is not None:
        cache.setdefault(record["ACCTID"], {})["record"] = record

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
            skip_trace_result = cached_call(
                cache, lead.acctid, "skip_trace", lambda: batchdata.skip_trace(address)
            )
            person = (skip_trace_result.get("results") or {}).get("persons", [{}])[0]
            phones = person.get("phoneNumbers", [])
            if phones:
                lead.phone = phones[0].get("number")
                lead.do_not_call = phones[0].get("dnc")
            lead.tcpa_risk = person.get("dnc", {}).get("tcpa")
            emails = person.get("emails", [])
            if emails:
                # Prefer an address BatchData has actually tested/verified
                # deliverable over an untested one, when both exist.
                tested = [e.get("email") for e in emails if e.get("tested")]
                lead.email = tested[0] if tested else emails[0].get("email")
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
            valuation_result = cached_call(
                cache, lead.acctid, "valuation", lambda: batchdata.lookup_valuation(address)
            )
            properties = (valuation_result.get("results") or {}).get("properties", [])
            if properties:
                prop = properties[0]
                lead.arv_estimate = prop.get("valuation", {}).get("estimatedValue")
                last_sale = (prop.get("sale") or {}).get("lastSale") or {}
                lead.last_sale_price = last_sale.get("price")
                last_sale_year = _year_from_iso_date(last_sale.get("saleDate"))
                lead.last_sale_date = str(last_sale_year) if last_sale_year else None
                owner_full_name = prop.get("owner", {}).get("fullName")
                if owner_full_name:
                    lead.owner_name = owner_full_name
                permit = prop.get("permit") or {}
                latest_permit_year = _year_from_iso_date(permit.get("latestDate"))
                if permit.get("permitCount") and latest_permit_year is not None:
                    permit_history = [{"year": latest_permit_year}]
                quick_lists = prop.get("quickLists") or {}
                lead.distress_flags = _distress_flags(quick_lists)
                lead.in_preforeclosure = _in_preforeclosure(quick_lists)
                lead.corporate_or_trust_owned = bool(
                    quick_lists.get("corporateOwned") or quick_lists.get("trustOwned")
                )
                # "Free and clear" (no open liens) means totalOpenLienBalance
                # is genuinely absent from BatchData's response rather than 0.
                lien_balance = (prop.get("openLien") or {}).get("totalOpenLienBalance")
                if lien_balance is None and quick_lists.get("freeAndClear"):
                    lien_balance = 0
                lead.total_lien_balance = lien_balance
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
        if lead.total_lien_balance is not None:
            lead.payoff_profit = payoff_profit_estimate(
                lead.arv_estimate, lead.total_lien_balance, lead.repair_cost_estimate
            )
            lead.high_priority = lead.payoff_profit > 0
    return lead


def _exclusion_reasons(lead: Lead, require_preforeclosure: bool = False) -> list[str]:
    """Reasons a lead isn't worth pursuing. Only flags what can actually be
    determined from data on hand — a lead with no valuation data at all
    (e.g. BatchData unavailable) isn't excluded on margin grounds, since
    there's nothing to judge it against."""
    reasons = []
    if lead.low_margin is True:
        reasons.append("low margin (assessed value >= BatchData estimated value)")
    if lead.mao_estimate is not None and lead.mao_estimate <= 0:
        reasons.append("max allowable offer <= $0")
    if not lead.phone and not lead.email:
        reasons.append("no phone or email found")
    if lead.corporate_or_trust_owned:
        reasons.append("corporate/trust owned")
    if require_preforeclosure and not lead.in_preforeclosure:
        reasons.append(
            "not currently in pre-foreclosure (no BatchData preforeclosure/"
            "notice-of-default/notice-of-sale/lis-pendens flag)"
        )
    return reasons


def filter_worth_pursuing(
    leads: list[Lead], require_preforeclosure: bool = False
) -> list[Lead]:
    """Drops leads flagged by _exclusion_reasons() from the digest. Callers
    should still mark every lead's ACCTID as seen regardless of this
    filter's outcome — a lead that isn't worth pursuing today was still
    paid for, so it shouldn't be re-enriched (re-billed) on a future run."""
    kept = []
    for lead in leads:
        reasons = _exclusion_reasons(lead, require_preforeclosure=require_preforeclosure)
        if reasons:
            print(f"Not pursuing {lead.acctid} ({lead.address}): {', '.join(reasons)}")
        else:
            kept.append(lead)
    return kept


def _format_currency(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "unknown"


def build_digest_body(leads: list[Lead]) -> str:
    body_lines = []
    for lead in leads:
        margin_note = ""
        if lead.low_margin is True:
            margin_note = " | LOW MARGIN (assessed value >= BatchData estimate)"
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
        payoff_line = (
            f"  Owes: {_format_currency(lead.total_lien_balance)} | "
            f"Profit if offer = payoff: {_format_currency(lead.payoff_profit)}"
            f"{' — HIGH PRIORITY' if lead.high_priority else ''}\n"
            if lead.total_lien_balance is not None
            else ""
        )
        distress_line = (
            f"  Distress signals: {', '.join(lead.distress_flags)}\n"
            if lead.distress_flags
            else ""
        )
        last_sale_line = (
            f"  Last sale: {_format_currency(lead.last_sale_price)} ({lead.last_sale_date})\n"
            if lead.last_sale_price is not None
            else ""
        )
        priority_prefix = ""
        if lead.in_preforeclosure:
            priority_prefix += "[PRE-FORECLOSURE] "
        if lead.high_priority:
            priority_prefix += "[HIGH PRIORITY] "
        script_lines = "".join(f"    - {line}\n" for line in call_script(lead))
        body_lines.append(
            f"{priority_prefix}{lead.address}\n"
            f"  Owner: {lead.owner_name} | Phone: {lead.phone or 'n/a'}{dnc_note}{tcpa_note} | "
            f"Email: {lead.email or 'n/a'} | "
            f"Signal: {lead.motivation_signal} | Condition: {lead.condition_tier}"
            f"{margin_note}\n"
            f"{distress_line}"
            f"{last_sale_line}"
            f"  County-assessed value: {_format_currency(lead.assessed_value)} | "
            f"Est. repair cost: {_format_currency(lead.repair_cost_estimate)}{repair_note} | "
            f"BatchData estimated value (AVM): {_format_currency(lead.arv_estimate)}\n"
            f"{mao_line}"
            f"{payoff_line}"
            f"  Zillow: {lead.zillow_link}\n"
            f"  Call prep (Price / Condition / Motivation / Time):\n"
            f"{script_lines}"
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
    parser.add_argument(
        "--preforeclosure",
        action="store_true",
        help=(
            "Target the pre-foreclosure window instead of absentee owners: "
            "includes owner-occupied properties (dropping the absentee-address "
            "filter) and only keeps leads BatchData flags as preforeclosure/"
            "notice-of-default/notice-of-sale/lis-pendens. MD iMap has no free "
            "way to pre-filter for foreclosure status the way it does for "
            "absentee ownership, so every gathered candidate still costs a "
            "BatchData call even if it turns out not to be in foreclosure — "
            "consider a lower BATCHDATA_MAX_ENRICHMENT_ATTEMPTS."
        ),
    )
    args = parser.parse_args()

    seen = load_seen_parcels()
    target_matches = int(
        os.environ.get("BATCHDATA_MAX_LEADS_PER_RUN", DEFAULT_MAX_LEADS_PER_RUN)
    )
    max_attempts = int(
        os.environ.get(
            "BATCHDATA_MAX_ENRICHMENT_ATTEMPTS",
            target_matches * DEFAULT_MAX_ENRICHMENT_ATTEMPTS_MULTIPLIER,
        )
    )
    max_attempts = max(max_attempts, target_matches)

    candidates = gather_new_leads(
        seen,
        args.jurisdictions,
        limit=max_attempts,
        require_absentee=not args.preforeclosure,
    )
    print(
        f"Gathered {len(candidates)} candidate lead(s) to try (up to "
        f"BATCHDATA_MAX_ENRICHMENT_ATTEMPTS={max_attempts}), looking for "
        f"{target_matches} worth pursuing"
        + (" in pre-foreclosure" if args.preforeclosure else "")
        + "."
    )

    api_token = os.environ.get("BATCHDATA_API_KEY")
    batchdata = BatchDataClient(api_token) if api_token else None
    if batchdata is None:
        print("BATCHDATA_API_KEY not set; skipping skip-trace/valuation.")

    # Enrichment is the billable part, so it stops as soon as target_matches
    # leads worth pursuing are found rather than enriching every gathered
    # candidate regardless of outcome. batchdata_cache records each raw
    # response so a future change to enrich_lead()'s extraction logic can
    # be replayed (scripts/reprocess_from_cache.py) without re-billing.
    cache = load_batchdata_cache()
    enriched: list[Lead] = []
    leads: list[Lead] = []
    for record in candidates:
        lead = enrich_lead(record, batchdata, cache=cache)
        enriched.append(lead)
        leads.extend(
            filter_worth_pursuing([lead], require_preforeclosure=args.preforeclosure)
        )
        if len(leads) >= target_matches:
            break
    save_batchdata_cache(cache)

    unenriched = len(candidates) - len(enriched)
    if unenriched:
        print(
            f"{unenriched} gathered candidate(s) left unenriched this run "
            f"(already found {target_matches} worth pursuing)."
        )
    elif len(leads) < target_matches:
        print(
            f"Only found {len(leads)} of {target_matches} worth pursuing "
            f"after trying every gathered candidate this run."
        )

    # Leads currently in pre-foreclosure surface first regardless of mode
    # (BatchData's flags are read every run, not just --preforeclosure
    # ones), then high-priority leads (profitable even offering just the
    # payoff amount) within each group.
    leads.sort(key=lambda lead: (not lead.in_preforeclosure, not lead.high_priority))

    archive = load_lead_archive()
    record_leads(archive, enriched)
    save_lead_archive(archive)

    qualified = load_qualified_leads()
    record_leads(qualified, leads)
    save_qualified_leads(qualified)

    if leads and not args.no_email:
        send_weekly_digest(leads, os.environ["DIGEST_EMAIL_TO"])
        print(f"Digest sent for {len(leads)} lead(s).")
    elif leads:
        print(f"{len(leads)} lead(s) ready; --no-email set, skipping send.")
    else:
        print("Nothing new this run.")

    # Only leads actually enriched count as seen — a gathered-but-untried
    # candidate (because target_matches was already reached) wasn't paid
    # for, so it stays available to try on a future run.
    seen.update(lead.acctid for lead in enriched)
    save_seen_parcels(seen)


if __name__ == "__main__":
    main()
