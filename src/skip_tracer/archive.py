"""Local-only archive of fully-enriched lead data (Zillow link, owner name,
phone, email, valuation, distress flags, condition, etc.).

Deliberately NOT GitHub-backed like state.py's seen-parcel list can be:
this data includes real property owners' names, phone numbers, and email
addresses, and this repo is public. Committing that would put third
parties' contact info into permanent, public git history. This file stays
local-only (gitignored, never synced anywhere) so a lost digest email
doesn't mean re-paying BatchData to recover the data — at the cost of not
surviving a host with an ephemeral disk (e.g. Render Cron without an
attached persistent disk) between runs. Point LEADS_ARCHIVE_PATH at a
mounted persistent disk there if this needs to survive on Render.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_ARCHIVE_FILE = Path(__file__).resolve().parent.parent.parent / "leads_archive.json"


def _archive_path() -> Path:
    override = os.environ.get("LEADS_ARCHIVE_PATH")
    return Path(override) if override else DEFAULT_ARCHIVE_FILE


def load_lead_archive() -> dict[str, Any]:
    path = _archive_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_lead_archive(archive: dict[str, Any]) -> None:
    path = _archive_path()
    content = json.dumps(archive, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def record_leads(archive: dict[str, Any], leads: list[Any]) -> dict[str, Any]:
    """Adds or overwrites each lead's entry in `archive`, keyed by ACCTID
    and stamped with when it was archived. Mutates and returns `archive`.
    Every enriched lead is worth archiving here regardless of whether it
    passed filter_worth_pursuing() — it was already paid for either way."""
    for lead in leads:
        entry = asdict(lead)
        entry["archived_at"] = datetime.now(timezone.utc).isoformat()
        archive[lead.acctid] = entry
    return archive
