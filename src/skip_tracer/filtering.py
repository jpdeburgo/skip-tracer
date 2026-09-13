"""Absentee-owner filtering and entity classification.

`OOI` alone produced a 28-36% false-positive rate for absentee status across
two test batches (same-address records still flagged non-owner-occupied),
so the real filter is a normalized owner-vs-property address comparison.
"""

from __future__ import annotations

import re
from functools import lru_cache

STREET_SUFFIX_MAP = {
    "LANE": "LN", "DRIVE": "DR", "COURT": "CT", "TERRACE": "TER",
    "ROAD": "RD", "STREET": "ST", "AVENUE": "AVE", "PLACE": "PL",
    "BOULEVARD": "BLVD", "CIRCLE": "CIR", "WAY": "WAY",
}

CO_PATTERN = re.compile(r"^C/?O\b", re.IGNORECASE)

# EXCLASS doesn't catch all diplomatic ownership — had to add "EMBSY" by
# hand after "EMBASSY" alone missed a real embassy-owned record.
DIPLOMATIC_KEYWORDS = ["EMBASSY", "EMBSY", "CONSULATE"]

NER_MODEL_NAME = "dslim/bert-base-NER"


def normalize_address(raw: str) -> str:
    """Collapse whitespace, uppercase, normalize common street-suffix variants."""
    if not raw:
        return ""
    cleaned = re.sub(r"\s+", " ", raw.strip().upper())
    for long_form, short_form in STREET_SUFFIX_MAP.items():
        cleaned = re.sub(rf"\b{long_form}\b", short_form, cleaned)
    return cleaned


def is_genuinely_absentee(record: dict) -> bool:
    """The real absentee filter — normalized address comparison, not OOI."""
    owner_addr = normalize_address(record.get("OWNADD1", ""))
    property_addr = normalize_address(record.get("ADDRESS", ""))
    return owner_addr != "" and owner_addr != property_addr


def contactability_tier(record: dict) -> str:
    """C/O mail-handler pattern = still a real lead, different tier."""
    owner_addr1 = record.get("OWNADD1", "") or ""
    if CO_PATTERN.match(owner_addr1.strip()):
        return "third_party_mail_handler"
    return "direct"


@lru_cache(maxsize=1)
def _get_ner_pipeline():
    """Lazily load the NER model — avoids paying the download/import cost
    for callers (like most tests) that never classify an owner entity."""
    from transformers import pipeline

    return pipeline("ner", model=NER_MODEL_NAME, grouped_entities=True)


def classify_owner_entity(record: dict) -> str:
    """Catches corporate/institutional/diplomatic ownership generally,
    instead of a manual keyword list that already proved to miss variants."""
    owner_text = f"{record.get('OWNADD1', '')} {record.get('OWNADD2', '')}".strip()
    if any(keyword in owner_text.upper() for keyword in DIPLOMATIC_KEYWORDS):
        return "diplomatic"
    if not owner_text:
        return "individual"
    entities = _get_ner_pipeline()(owner_text)
    if any(entity["entity_group"] == "ORG" for entity in entities):
        return "organization"
    return "individual"
