"""MD iMap ArcGIS property search — free, no auth required.

Query refined over multiple rounds against real Montgomery/Prince George's
County data to minimize false positives: OOI<>'H' alone over-matches, so the
where-clause also requires a mailing address, drops exempt-class parcels,
requires an improvement value (land-only/vacant parcels excluded), and drops
current-use-code parcels.
"""

from __future__ import annotations

import requests

MD_IMAP_URL = (
    "https://mdgeodata.md.gov/imap/rest/services/PlanningCadastre/"
    "MD_PropertyData/MapServer/0/query"
)

# Process MONT and PRIN first — everything else is lower priority but still
# in scope.
PRIORITY_JURISDICTIONS = ["MONT", "PRIN"]
ALL_JURISDICTIONS = [
    "ALLE", "ANNE", "BACI", "BACO", "CALV", "CARO", "CARR", "CECI", "CHAR",
    "DORC", "FRED", "GARR", "HARF", "HOWA", "KENT", "MONT", "PRIN", "QUEE",
    "SOME", "STMA", "TALB", "WASH", "WICO", "WORC",
]
PROCESSING_ORDER = PRIORITY_JURISDICTIONS + [
    j for j in ALL_JURISDICTIONS if j not in PRIORITY_JURISDICTIONS
]

PAGE_SIZE = 1000
REQUEST_TIMEOUT_SECONDS = 30


def fetch_jurisdiction_leads(
    jurs_code: str,
    session: requests.Session | None = None,
    include_owner_occupied: bool = False,
) -> list[dict]:
    """Pull all candidate leads for one MD jurisdiction.

    Paginates past the server's per-request transfer limit using
    `exceededTransferLimit`, since the server caps returned records well
    below any `resultRecordCount` we ask for.

    By default drops owner-occupied parcels (`OOI<>'H'`) since the weekly
    absentee-owner pipeline only wants those. Pass `include_owner_occupied`
    for the pre-foreclosure pipeline instead: a homeowner behind on their
    own mortgage is still living in the property, so excluding OOI='H'
    would filter out exactly the sellers that mode is looking for.
    """
    conditions = [f"JURSCODE='{jurs_code}'"]
    if not include_owner_occupied:
        conditions.append("OOI<>'H'")
    conditions += [
        "ADDRESS IS NOT NULL",
        "EXCLASS IS NULL",
        "NFMIMPVL IS NOT NULL",
        "CIUSE IS NULL",
    ]
    where_clause = " AND ".join(conditions)
    http = session or requests
    all_records: list[dict] = []
    offset = 0

    while True:
        params = {
            "where": where_clause,
            "outFields": "*",
            "f": "json",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
        }
        response = http.get(MD_IMAP_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()

        features = data.get("features", [])
        all_records.extend(feature["attributes"] for feature in features)

        if not data.get("exceededTransferLimit") or not features:
            break
        offset += len(features)

    return all_records
