"""Local-only archives of enriched lead data.

Two files, both keyed by ACCTID and stamped with when each entry was
recorded:

- leads_archive.json: every enriched lead, regardless of whether it passed
  filter_worth_pursuing() — it was already paid for either way.
- qualified_leads.json: only the leads that passed and were sent in a
  digest — a standing record of "houses deemed profitable" independent of
  the email itself.

Neither is GitHub-backed like state.py's seen-parcel list can be: this data
includes real property owners' names, phone numbers, and email addresses,
and this repo is public. Committing that would put third parties' contact
info into permanent, public git history. Both files stay local-only
(gitignored, never synced anywhere) so a lost digest email doesn't mean
re-paying BatchData to recover the data — at the cost of not surviving a
host with an ephemeral disk (e.g. Render Cron without an attached
persistent disk) between runs. Point LEADS_ARCHIVE_PATH/QUALIFIED_LEADS_PATH
at a mounted persistent disk there if either needs to survive on Render.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ARCHIVE_FILE = _REPO_ROOT / "leads_archive.json"
DEFAULT_QUALIFIED_LEADS_FILE = _REPO_ROOT / "qualified_leads.json"


def _resolve_path(env_var: str, default: Path) -> Path:
    override = os.environ.get(env_var)
    return Path(override) if override else default


def _archive_path() -> Path:
    return _resolve_path("LEADS_ARCHIVE_PATH", DEFAULT_ARCHIVE_FILE)


def _qualified_leads_path() -> Path:
    return _resolve_path("QUALIFIED_LEADS_PATH", DEFAULT_QUALIFIED_LEADS_FILE)


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json_dict(path: Path, data: dict[str, Any]) -> None:
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def load_lead_archive() -> dict[str, Any]:
    return _load_json_dict(_archive_path())


def save_lead_archive(archive: dict[str, Any]) -> None:
    _save_json_dict(_archive_path(), archive)


def load_qualified_leads() -> dict[str, Any]:
    return _load_json_dict(_qualified_leads_path())


def save_qualified_leads(qualified: dict[str, Any]) -> None:
    _save_json_dict(_qualified_leads_path(), qualified)


def record_leads(archive: dict[str, Any], leads: list[Any]) -> dict[str, Any]:
    """Adds or overwrites each lead's entry in `archive`, keyed by ACCTID
    and stamped with when it was recorded. Mutates and returns `archive`."""
    for lead in leads:
        entry = asdict(lead)
        entry["archived_at"] = datetime.now(timezone.utc).isoformat()
        archive[lead.acctid] = entry
    return archive
