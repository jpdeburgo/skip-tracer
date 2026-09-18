"""Local-only cache of raw BatchData responses, keyed by ACCTID.

Each entry holds the exact MD iMap `record` a lead was built from, plus the
raw skip_trace and lookup_valuation responses BatchData returned for it.
Lets enrich_lead() re-derive Lead fields from an already-paid-for response
without re-billing BatchData — e.g. after changing what gets extracted or
saved to leads_archive.json/qualified_leads.json (see
scripts/reprocess_from_cache.py, which replays every cached lead through
the current enrichment logic with zero new API calls).

This cache only helps going forward from when it was added — leads
processed before it existed were never cached, so they can't be replayed.

Same never-synced-to-GitHub reasoning as archive.py, more so: this is the
*raw* BatchData response, which includes more complete personal data than
even leads_archive.json's already-extracted subset (full demographics like
income/net worth, complete mortgage history, etc.). Local-only, gitignored.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

DEFAULT_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "batchdata_cache.json"


def _cache_path() -> Path:
    override = os.environ.get("BATCHDATA_CACHE_PATH")
    return Path(override) if override else DEFAULT_CACHE_FILE


def load_batchdata_cache() -> dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_batchdata_cache(cache: dict[str, Any]) -> None:
    path = _cache_path()
    content = json.dumps(cache, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def cached_call(
    cache: dict[str, Any] | None, acctid: str, key: str, make_call: Callable[[], Any]
) -> Any:
    """Returns cache[acctid][key] if already present, otherwise calls
    make_call(), stores the result, and returns it. A cache of None means
    "no caching" — make_call() runs every time, unconditionally."""
    if cache is None:
        return make_call()
    entry = cache.setdefault(acctid, {})
    if key not in entry:
        entry[key] = make_call()
    return entry[key]
