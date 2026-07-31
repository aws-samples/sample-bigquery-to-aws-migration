"""Bundle data model — the typed hand-off artifact."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from bq_assess.models import (
    EntityMetadata,
    FailureRecord,
    PricingDetection,
    SlotUtilization,
)

# v2 (2026-07-28): multi-region collection — manifest gains "regions" (all dataset
# locations found); bq_location becomes the PRIMARY region (most datasets). The
# loader accepts v1 bundles (regions defaults to [bq_location]).
SCHEMA_VERSION = 2
COMPATIBLE_SCHEMA_VERSIONS = frozenset({1, 2})


def sha256_file(path: str | Path) -> str:
    """Checksum a file — the bundle's integrity contract, shared by writer + loader."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class QueryRecord:
    """One anonymized query + per-job stats (queries.jsonl row)."""
    query: str
    total_slot_ms: int = 0
    total_bytes_processed: int = 0
    total_bytes_billed: int | None = None  # None = column unavailable
    statement_type: str | None = None
    creation_time: str | None = None


@dataclass
class Bundle:
    """The complete hand-off artifact between collector and report generator."""
    project_id: str
    bq_location: str                # PRIMARY region (most datasets) — pricing anchor
    aws_region: str
    entities: list[EntityMetadata]
    failures: list[FailureRecord] = field(default_factory=list)
    workload: SlotUtilization | None = None
    pricing: PricingDetection | None = None
    rates: dict | None = None  # PricingRates serialized via price_lookup.rates_to_dict
    queries: list[QueryRecord] | None = None
    storage_basis: str = "assumed"  # measured | mixed | assumed (from StorageStats.basis)
    collector_version: str = ""
    created_at: str = ""
    regions: list[str] = field(default_factory=list)  # ALL dataset locations (v2)

    def __post_init__(self) -> None:
        # Single home for the v1-compat rule: regions is NEVER empty — a legacy
        # bundle (or in-memory construction) defaults to [bq_location]. Enforced
        # here so writer/loader/report consumers read bundle.regions directly
        # instead of re-deriving the fallback (2026-07-28 review: three sites
        # each had their own copy, already divergent).
        if not self.regions:
            self.regions = [self.bq_location]
