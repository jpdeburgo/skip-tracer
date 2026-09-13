"""BatchData API client — skip trace, valuation, permits, and compliance.

Base URL: https://api.batchdata.com — the version prefix /api/v1/ is
required and easy to miss; a condensed reference we worked from omitted it,
causing a 404 on a route that otherwise looked correct.

skip_trace() and lookup_valuation() are UNVERIFIED: the request/response
shapes below are a best guess based on the `requests`/`options` pattern used
elsewhere in BatchData's API. Run scripts/test_skip_trace.py and
scripts/test_valuation.py against a real, already-known lead and adjust the
field names here before trusting either in the pipeline (see the
Test-First Checklist in README.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
        """`dataset: "valuation"` is one of 14 documented dataset/projection
        options alongside basic/core/foreclosure. Confirm whether the
        response is a single AVM figure or raw comps — that determines
        whether estimate_arv_from_comps() below is even needed."""
        return self._post(
            "property/lookup/all-attributes",
            {"requests": [{"address": address.as_dict()}], "dataset": "valuation"},
        )

    def get_property_permits(self, address: PropertyAddress) -> list[dict[str, Any]]:
        """Each property requested counts as one billable request
        regardless of how many permits come back."""
        result = self._post(
            "property/get-property-permits",
            {"requests": [{"address": address.as_dict()}]},
        )
        return result.get("results", {}).get("permits", [])

    def check_dnc(self, phone: str) -> dict[str, Any]:
        """Run on every phone number from skip-trace before it goes
        anywhere near a calling/texting workflow."""
        return self._post("phone/dnc", {"phoneNumbers": [phone]})

    def check_tcpa(self, phone: str) -> dict[str, Any]:
        """Run on every phone number from skip-trace before it goes
        anywhere near a calling/texting workflow."""
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


def estimate_arv_from_comps(comps: list[dict[str, Any]], subject_sqft: float) -> float:
    """Recency-weighted average $/sqft across comps, applied to the subject's
    square footage. Only needed if the valuation dataset returns raw comps
    rather than a single AVM figure — skip this and use that figure
    directly if it doesn't.

    comps: list of {"price": float, "sqft": float, "sale_date": str}
    """
    weighted_price_per_sqft = []
    for comp in comps:
        price_per_sqft = comp["price"] / comp["sqft"]
        days_old = (datetime.now() - datetime.fromisoformat(comp["sale_date"])).days
        weight = max(1, 365 - days_old) / 365
        weighted_price_per_sqft.append((price_per_sqft, weight))

    total_weight = sum(weight for _, weight in weighted_price_per_sqft)
    avg_price_per_sqft = (
        sum(price * weight for price, weight in weighted_price_per_sqft)
        / total_weight
    )
    return avg_price_per_sqft * subject_sqft
