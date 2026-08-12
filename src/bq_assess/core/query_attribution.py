"""Query attribution: map JOBS log queries to the tables they reference.

Table-reference parsing is delegated to core/table_refs.extract_dataset_tables
(the shared grammar — 2026-08-04 consolidation). Each query is attributed to
every table it references — a JOIN query touching 3 tables appears in all 3
entities' workload; cross-project refs are dropped by the shared grammar.

The output is a per-entity workload summary (query count, total slot cost,
statement types) plus the top-N query samples for the report's side-by-side
translation view and the full shape list for the query-workload sidecar.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from bq_assess.bundle.models import QueryRecord
from bq_assess.core.table_refs import extract_dataset_tables

# Shapes embedded in the HTML per entity (2026-08-04: was 5 with a 200-entity
# estate cap; now 3 with NO entity cap — every attributed row gets its top
# queries inline, the full set lives in the query-workload/ sidecar).
TOP_QUERIES_PER_ENTITY = 3


@dataclass
class QuerySample:
    """One query attributed to an entity — carried into the report."""
    query: str
    statement_type: str
    total_slot_ms: int


@dataclass
class EntityWorkload:
    """Aggregated workload for one entity from query logs."""
    query_count: int = 0
    total_slot_ms: int = 0
    statement_types: dict[str, int] = field(default_factory=dict)
    samples: list[QuerySample] = field(default_factory=list)
    num_shapes: int = 0  # distinct query texts seen, before the top-N sample cap
    # ALL distinct shapes by slot cost (samples is its top-N prefix). Feeds the
    # per-entity workload sidecar files; never embedded in the HTML report.
    all_shapes: list[QuerySample] = field(default_factory=list)

    @property
    def slot_hours(self) -> float:
        return round(self.total_slot_ms / 3_600_000, 1)


def _extract_tables(query: str, project_id: str) -> set[str]:
    """dataset.table refs belonging to ``project_id`` (shared grammar).

    Cross-project references are dropped by extract_dataset_tables —
    attributing another project's workload to a same-named local entity
    inflates its stats with queries that will not migrate with it.
    """
    return extract_dataset_tables(query, project_id)


def attribute_queries(
    queries: list[QueryRecord],
    known_entities: set[str],
    project_id: str,
) -> dict[str, EntityWorkload]:
    """Attribute query log entries to known entities.

    Args:
        queries: Anonymized query records from the bundle.
        known_entities: Set of full_name strings (dataset.table) from scanned entities.
        project_id: The GCP project ID (used to strip project prefix from refs).

    Returns:
        Dict of entity full_name → EntityWorkload (only for entities that appear in logs).
    """
    workloads: dict[str, EntityWorkload] = defaultdict(EntityWorkload)
    entity_queries: dict[str, list[QueryRecord]] = defaultdict(list)

    for qr in queries:
        if not qr.query:
            continue
        tables = _extract_tables(qr.query, project_id)
        matched = tables & known_entities
        for entity_name in matched:
            entity_queries[entity_name].append(qr)

    for entity_name, qrs in entity_queries.items():
        wl = workloads[entity_name]
        wl.query_count = sum(1 for _ in qrs)
        # `or 0` guards records built outside the loader (which coerces null)
        wl.total_slot_ms = sum(qr.total_slot_ms or 0 for qr in qrs)
        for qr in qrs:
            st = qr.statement_type or "UNKNOWN"
            wl.statement_types[st] = wl.statement_types.get(st, 0) + 1

        sorted_qrs = sorted(qrs, key=lambda q: q.total_slot_ms or 0, reverse=True)
        seen_text: set[str] = set()
        for qr in sorted_qrs:
            normalized = qr.query.strip()
            if normalized in seen_text:
                continue
            seen_text.add(normalized)
            wl.all_shapes.append(QuerySample(
                query=qr.query,
                statement_type=qr.statement_type or "UNKNOWN",
                total_slot_ms=qr.total_slot_ms or 0,
            ))
        wl.num_shapes = len(seen_text)
        wl.samples = wl.all_shapes[:TOP_QUERIES_PER_ENTITY]

    # No per-estate entity cap (2026-08-04): EVERY attributed entity embeds its
    # top-N samples. Report size is bounded per entity instead — N=3 shapes,
    # each shape's SQL clipped at MAX_SAMPLE_SQL_CHARS by the serializer. The
    # full untruncated shape set always lives in the query-workload/ sidecar.
    return dict(workloads)
