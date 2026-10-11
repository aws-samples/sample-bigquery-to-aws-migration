"""Read real partition counts and bounds from INFORMATION_SCHEMA.PARTITIONS (#53).

Before this existed, the Athena migration generator *guessed* a table's partition
count as ``(now - last_modified).days × partitions_per_day``. ``last_modified`` is
the last **write** time, so for any actively-ingested table it is today and the
guess collapses toward zero — chunking is skipped and a single INSERT tries to
open every partition at once, past Athena's ~100-open-partition write limit. The
failure was in the unsafe direction, and it hit the busiest tables hardest:
measured on a 24-month testbed, five tables holding 730-731 partitions were
estimated at 30.

Two things differ from ``storage_stats.py``, whose shape this otherwise follows:

- ``PARTITIONS`` is **dataset-scoped**. The ``region-<loc>`` qualified form that
  TABLE_STORAGE uses returns 403 here (probe-verified), so this issues one query
  per dataset rather than one per region. Datasets without partitioned tables
  still cost a query, so only datasets holding sized tables are read.
- Non-partitioned tables return a single row whose ``partition_id`` is SQL NULL,
  so ``WHERE partition_id IS NOT NULL`` leaves only genuinely partitioned tables.
  BigQuery's ``__NULL__`` / ``__UNPARTITIONED__`` sentinels are strings and would
  survive that filter, so they are excluded explicitly — a ``__NULL__`` partition
  is real data but is not a date and must not skew the bounds. (The migration
  generator emits a dedicated IS NULL chunk for those rows.)

Unreadable view or missing permission degrades to ``None``, and callers fall back
to the old estimate rather than failing the run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bq_assess.core.jobs_query import _retry_query

logger = logging.getLogger(__name__)

# BigQuery partition_id widths per granularity — probe-verified against a live
# dataset holding one table per granularity:
#   YEAR "2025" | MONTH "202503" | DAY "20250307" | HOUR "2025030713"
_ID_WIDTH_TO_GRANULARITY = {4: "YEAR", 6: "MONTH", 8: "DAY", 10: "HOUR"}

# partition_id values that are real partitions but not dates.
_SENTINEL_IDS = ("__NULL__", "__UNPARTITIONED__")


@dataclass
class PartitionInfo:
    count: int
    min_id: str
    max_id: str


@dataclass
class PartitionStats:
    # full_name → PartitionInfo, only for tables with at least one dated partition
    partitions: dict[str, PartitionInfo] = field(default_factory=dict)
    measured_datasets: int = 0
    failed_datasets: int = 0
    failure_reason: str = ""       # "" = every dataset read cleanly

    @property
    def basis(self) -> str:
        if self.measured_datasets and not self.failed_datasets:
            return "measured"
        if self.measured_datasets:
            return "mixed"
        return "unavailable"

    @property
    def source_note(self) -> str:
        if self.basis == "measured":
            return (
                f"Partition counts measured from INFORMATION_SCHEMA.PARTITIONS "
                f"({len(self.partitions)} partitioned tables across "
                f"{self.measured_datasets} dataset(s))."
            )
        if self.basis == "mixed":
            return (
                f"Partial PARTITIONS coverage — {self.measured_datasets} dataset(s) "
                f"read, {self.failed_datasets} failed; chunking for unread datasets "
                f"falls back to estimating from last_modified."
            )
        return (
            "INFORMATION_SCHEMA.PARTITIONS unavailable — partition counts estimated "
            "from last_modified, which under-counts actively-written tables and can "
            "skip required chunking."
        )


def resolve_partition_counts(
    client, project_id: str, location: str | dict[str, list[str]], entities
) -> PartitionStats:
    """Read partition counts/bounds for every dataset holding sized tables.

    ``location`` matches ``resolve_physical_bytes``: a single region, or a
    ``{region: [dataset_ids]}`` map. It is used only to pin each job to its
    data's region — the view itself is addressed per dataset.
    """
    if isinstance(location, str):
        region_datasets = {
            location: sorted({e.dataset_id for e in entities if e.num_bytes})
        }
    else:
        region_datasets = location

    # A dataset is worth querying only if it holds a sized, partitioned table.
    # Reading PARTITIONS for a dataset of views or unpartitioned tables is a
    # wasted job on a large estate.
    worth_reading: dict[str, set] = {}
    for entity in entities:
        if not entity.num_bytes:
            continue
        if entity.time_partitioning is None and entity.range_partitioning is None:
            continue
        for loc, dataset_ids in region_datasets.items():
            if entity.dataset_id in dataset_ids:
                worth_reading.setdefault(loc, set()).add(entity.dataset_id)
                break

    stats = PartitionStats()
    failure_reasons: list[str] = []
    for loc, dataset_ids in sorted(worth_reading.items()):
        for dataset_id in sorted(dataset_ids):
            try:
                stats.partitions.update(
                    _query_partitions(client, project_id, dataset_id, loc)
                )
                stats.measured_datasets += 1
            except Exception as exc:
                # Must survive into failures.json. A silent degrade here is what
                # let the guessed count reach production in the first place.
                stats.failed_datasets += 1
                failure_reasons.append(f"[{loc}/{dataset_id}] {type(exc).__name__}: {exc}")
                logger.warning(
                    "Could not read INFORMATION_SCHEMA.PARTITIONS (%s.%s): %s",
                    project_id, dataset_id, exc,
                )
    stats.failure_reason = "; ".join(failure_reasons)
    return stats


def _query_partitions(
    client, project_id: str, dataset_id: str, location: str
) -> dict[str, PartitionInfo]:
    """Return {full_name: PartitionInfo} for one dataset's partitioned tables."""
    sentinels = ", ".join(f"'{s}'" for s in _SENTINEL_IDS)
    sql = (
        f"SELECT table_name, COUNT(*) AS partitions, "
        f"MIN(partition_id) AS min_id, MAX(partition_id) AS max_id "
        f"FROM `{project_id}`.`{dataset_id}`.INFORMATION_SCHEMA.PARTITIONS "
        f"WHERE partition_id IS NOT NULL AND partition_id NOT IN ({sentinels}) "
        f"GROUP BY table_name"
    )
    # Pin the job to the data's region, same reason as TABLE_STORAGE: relying on
    # the client default works only when the two happen to match.
    rows = _retry_query(lambda: client.query(sql, location=location).result())
    result: dict[str, PartitionInfo] = {}
    for row in rows:
        count = int(row.partitions or 0)
        if not count or not row.min_id:
            continue
        result[f"{dataset_id}.{row.table_name}"] = PartitionInfo(
            count=count, min_id=str(row.min_id), max_id=str(row.max_id),
        )
    return result


def granularity_for_partition_id(partition_id: str | None) -> str | None:
    """Infer BigQuery partition granularity from an id's width, or None."""
    if not partition_id:
        return None
    return _ID_WIDTH_TO_GRANULARITY.get(len(partition_id))


def partition_id_to_date(partition_id: str | None) -> str | None:
    """Convert a BigQuery partition_id to an ISO date (``YYYY-MM-DD``).

    Widths map to YEAR/MONTH/DAY/HOUR; shorter ids are widened to the first
    instant of their period, which is what a chunk window's lower bound wants.
    HOUR ids drop the hour component — windows are expressed in whole days.
    Returns None for sentinels and anything unrecognised, so callers can fall
    back rather than emit a malformed DATE literal.
    """
    if not partition_id or partition_id in _SENTINEL_IDS:
        return None
    if not partition_id.isdigit():
        return None
    if len(partition_id) not in _ID_WIDTH_TO_GRANULARITY:
        return None
    year = partition_id[:4]
    month = partition_id[4:6] or "01"
    day = partition_id[6:8] or "01"
    # Guard against a malformed id producing an invalid DATE literal downstream.
    if not ("01" <= month <= "12" and "01" <= day <= "31"):
        return None
    return f"{year}-{month}-{day}"
