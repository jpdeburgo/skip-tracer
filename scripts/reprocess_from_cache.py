"""Re-derive leads_archive.json and qualified_leads.json entries for every
cached lead in batchdata_cache.json, using the *current*
enrich_lead()/filter_worth_pursuing() logic — with zero new BatchData or MD
iMap calls. Merges into the existing archive files (by ACCTID) rather than
replacing them, so leads processed before caching existed are untouched.

Use this after changing what enrich_lead() extracts or how
filter_worth_pursuing() judges a lead (e.g. a new field, a new exclusion
rule), to see what the current logic would have produced for every
previously-cached lead without re-paying for skip-trace/valuation.

Only covers leads enriched *after* batchdata_cache.json was introduced —
anything processed before that was never cached and can't be replayed.
qualified_leads.json is updated additively: a lead that no longer passes
filter_worth_pursuing() under the new logic isn't removed from a prior
qualifying run, only leads that now pass are added/refreshed.

Usage:
    PYTHONPATH=src pipenv run python scripts/reprocess_from_cache.py
"""

from __future__ import annotations

from dotenv import load_dotenv

from skip_tracer.archive import (
    load_lead_archive,
    load_qualified_leads,
    record_leads,
    save_lead_archive,
    save_qualified_leads,
)
from skip_tracer.batchdata_cache import load_batchdata_cache
from skip_tracer.cli import enrich_lead, filter_worth_pursuing

load_dotenv()


class _NoNetworkBatchData:
    """Stands in for a real BatchDataClient during replay. Raising on any
    call (instead of quietly returning nothing) makes a cache-miss loud
    rather than silently producing an under-enriched lead."""

    def skip_trace(self, address):
        raise RuntimeError(
            "Cache miss on skip_trace during replay - refusing to call "
            "BatchData live. This ACCTID wasn't cached (processed before "
            "batchdata_cache.json existed, or the cache file is stale)."
        )

    def lookup_valuation(self, address):
        raise RuntimeError(
            "Cache miss on lookup_valuation during replay - refusing to "
            "call BatchData live. This ACCTID wasn't cached (processed "
            "before batchdata_cache.json existed, or the cache file is stale)."
        )


def main() -> None:
    cache = load_batchdata_cache()
    if not cache:
        print("batchdata_cache.json is empty - nothing to replay.")
        return

    fake_batchdata = _NoNetworkBatchData()
    all_leads = []
    for acctid, entry in cache.items():
        record = entry.get("record")
        if record is None:
            print(f"Skipping {acctid}: no cached record to replay from.")
            continue
        all_leads.append(enrich_lead(record, fake_batchdata, cache=cache))

    print(f"Replayed {len(all_leads)} lead(s) from cache.")

    # Loaded, not fresh: this only updates the entries for leads that were
    # actually replayed here, leaving any pre-caching archive entries alone.
    archive = load_lead_archive()
    record_leads(archive, all_leads)
    save_lead_archive(archive)

    qualified = load_qualified_leads()
    record_leads(qualified, filter_worth_pursuing(all_leads))
    save_qualified_leads(qualified)

    print(
        f"Rewrote leads_archive.json ({len(archive)} lead(s)) and updated "
        f"qualified_leads.json from the replay."
    )


if __name__ == "__main__":
    main()
