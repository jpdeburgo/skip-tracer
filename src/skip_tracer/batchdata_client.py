"""BatchData API client — skip trace, valuation, permits, and compliance.

Base URL: https://api.batchdata.com — the version prefix /api/v1/ is
required and easy to miss; a condensed reference we worked from omitted it,
causing a 404 on a route that otherwise looked correct.

skip_trace() and lookup_valuation() request/response shapes are VERIFIED
against a real lead (11132 Willowbrook Dr, Potomac, MD 20854) via
scripts/test_skip_trace.py and scripts/test_valuation.py:

- lookup_valuation() returns a single AVM figure
  (results.properties[0].valuation.estimatedValue), not raw comps, so no
  comp-weighting helper is needed here.
- It also returns owner name(s) (results.properties[0].owner.fullName), a
  rolled-up permit summary (results.properties[0].permit), and a
  results.properties[0].quickLists object of real distress/motivation
  booleans (vacant, taxDefault, preforeclosure, inherited, tiredLandlord,
  freeAndClear, highEquity/lowEquity, etc.) — cli.enrich_lead() uses the
  permit summary instead of a separate get_property_permits() call, and
  surfaces quickLists directly, all from this one call.
- The request's options.datasets field (documented at
  developer.batchdata.com) is meant to scope which of BatchData's ~14
  dataset categories come back. Tested requesting
  options.datasets=["valuation","owner","permit","quicklist"] against this
  account's token: the response was identical to not specifying datasets
  at all (still every category). Either this token's plan always returns
  everything regardless of the field, or the field isn't enforced — either
  way, don't rely on it to reduce payload size or cost for this account.
- skip_trace()'s top-level results.persons[0].name is a resident at the
  owner's mailing address, not necessarily the deed owner — that's
  results.persons[0].property.owner.name. Its phoneNumbers[] entries each
  carry their own "dnc" boolean, and the person carries a "dnc": {"tcpa":
  bool} litigation-risk flag — both are already included in this same
  call, which is why cli.enrich_lead() doesn't call check_dnc()/
  check_tcpa() below for the weekly digest (a human reviews it before any
  outreach). Those two methods stay available here for a future workflow
  that actually places calls/texts, per BatchData's own compliance
  guidance to check every number immediately before outreach.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

BATCHDATA_BASE_URL = "https://api.batchdata.com/api/v1"
REQUEST_TIMEOUT_SECONDS = 30


@dataclass
class PropertyAddress:
    street: str
    city: str
    state: str
    zipcode: str

    def as_dict(self) -> dict[str, str]:
        return {
            "street": self.street,
            "city": self.city,
            "state": self.state,
            "zip": self.zipcode,
        }


class BatchDataClient:
    """Thin wrapper around BatchData's REST API.

    Requires the $50 minimum wallet balance to be funded first — confirmed
    via a live 403 "Insufficient balance" response with $0 in the wallet.
    No free trial exists.
    """

    def __init__(self, api_token: str, session: requests.Session | None = None):
        self.api_token = api_token
        self.session = session or requests.Session()

    def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{BATCHDATA_BASE_URL}/{path}",
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            },
            json=json_body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()

    def skip_trace(self, address: PropertyAddress) -> dict[str, Any]:
        """V1 returns 1 person/property, V3 returns up to 3. Each request
        can handle up to 100 properties; cost scales with how many are
        submitted in one call."""
        return self._post(
            "property/skip-trace",
            {"requests": [{"propertyAddress": address.as_dict()}], "options": {}},
        )

    def lookup_valuation(self, address: PropertyAddress) -> dict[str, Any]:
        """options.datasets is BatchData's documented field for scoping
        which dataset categories come back (see this module's docstring for
        why it doesn't appear to narrow anything for this account) —
        request exactly what cli.enrich_lead() reads."""
        return self._post(
            "property/lookup/all-attributes",
            {
                "requests": [
                    {
                        "address": address.as_dict(),
                        "options": {
                            "datasets": ["valuation", "owner", "permit", "quicklist"]
                        },
                    }
                ]
            },
        )

    def get_property_permits(self, address: PropertyAddress) -> list[dict[str, Any]]:
        """Not called by cli.enrich_lead() — lookup_valuation()'s embedded
        permit summary covers condition_tier()'s needs at no extra cost.
        Kept for a future need of the full per-permit list. Each property
        requested counts as one billable request regardless of how many
        permits come back."""
        result = self._post(
            "property/get-property-permits",
            {"requests": [{"address": address.as_dict()}]},
        )
        return result.get("results", {}).get("permits", [])

    def check_dnc(self, phone: str) -> dict[str, Any]:
        """Not called by cli.enrich_lead() — skip_trace()'s response
        already includes a per-phone "dnc" flag, which is sufficient for a
        digest a human reviews before calling. Run this immediately before
        any actual call/text, per BatchData's compliance guidance, since
        registry status can change after skip-trace runs."""
        return self._post("phone/dnc", {"phoneNumbers": [phone]})

    def check_tcpa(self, phone: str) -> dict[str, Any]:
        """Not called by cli.enrich_lead() — skip_trace()'s response
        already includes a person-level TCPA litigation-risk flag
        (dnc.tcpa), which is sufficient for a digest a human reviews before
        calling. Run this immediately before any actual call/text, per
        BatchData's compliance guidance."""
        return self._post("phone/tcpa", {"phoneNumbers": [phone]})


def max_allowable_offer(arv: float, repair_cost: float) -> float:
    """Standard flipper's math — 0.70 covers closing/holding costs,
    commissions, profit margin.

    Deterministic, not a model call — decided explicitly, don't revisit
    without a strong reason. ARV is the one number a purchase decision
    hangs on; auditable math beats an LLM here for the same reason address
    normalization does (reproducible, no hallucination risk, cheaper).
    """
    return (arv * 0.70) - repair_cost


def flag_low_margin(assessed_value: float, arv_estimate: float) -> bool:
    """If the county's own assessment already meets/exceeds ARV, there's
    no room for a flip."""
    return assessed_value >= arv_estimate


def payoff_profit_estimate(
    arv_estimate: float, total_lien_balance: float, repair_cost: float
) -> float:
    """Profit potential if the offer is just enough to cover the owner's
    existing lien balance — a distressed seller's real floor (what they
    need to avoid a deficiency at foreclosure), not an abstract flip
    margin. Positive means there's room to profit even without any
    below-market discount from the owner, which is a stronger signal for
    this pipeline's purpose than the 70%-rule MAO alone.

    Ignores closing/holding/resale costs that max_allowable_offer()'s 0.70
    factor already bakes in — treat this as a prioritization signal, not a
    number to actually offer.
    """
    return arv_estimate - total_lien_balance - repair_cost
