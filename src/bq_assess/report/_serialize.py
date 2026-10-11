"""Shared serialization: Assessment dataclass → dicts for JSON/HTML consumption."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum

from bq_assess.engine.redshift import cost_constants as k
from bq_assess.models import Assessment, EntityPopulation
from bq_assess.report.provenance import provenance


def _to_dict(obj):
    """Recursively serialize a dataclass/enum/datetime tree to JSON-safe primitives.

    Rules:
    - Enum → .value
    - datetime → .isoformat()
    - Decimal → float
    - dataclass → dict (None-valued fields omitted)
    - list → list (recurse each element)
    - dict → dict (recurse values, omit None values)
    - primitives pass through
    """
    if obj is None:
        return None
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            k: v
            for k, v in (
                (f.name, _to_dict(getattr(obj, f.name)))
                for f in dataclasses.fields(obj)
            )
            if v is not None
        }
    if isinstance(obj, list):
        return [_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: v for k, v in ((k, _to_dict(v)) for k, v in obj.items()) if v is not None}
    return obj


def serialize_landing(assessment: Assessment) -> dict:
    """Build the landing JSON dict: metadata + summary + cost + failures + engine fields.

    Identity fields come from ``provenance()`` so every artifact carries the same
    block. ``schema_version`` lets external consumers gate on field semantics —
    added 2026-07-16 when ``cost.scope_notes`` changed meaning under the same key
    — and is an integer bumped by hand, NOT the package version, which could
    never signal a contract change. See report/provenance.py.

    migration_plans is serialized as a SUMMARY (counts only) at the landing level —
    the full per-entity plans live in the effort JSON rows (Fix 7: kill double
    serialization; per-entity is the canonical copy the report reads).
    """
    # Slim migration_plans to a summary instead of full plans (Fix 7)
    migration_summary = None
    if assessment.migration_plans:
        plans = assessment.migration_plans
        chunked = sum(
            1 for p in plans.values()
            if p.statements and len(p.statements) > 1
        )
        action_required = sum(
            1 for p in plans.values()
            if any(s.severity == "action_required" for s in p.shortcomings)
        )
        # Split the action_required count (#68). "This load cannot run as
        # generated" and "review the emitted cast" are very different asks, and
        # one number covering both misleads planning — a reader cannot tell
        # whether 7 means 7 broken loads or 7 casts to eyeball. action_required
        # is retained as the total so existing consumers keep working.
        load_blocking = sum(
            1 for p in plans.values()
            if any(s.blocks_load for s in p.shortcomings)
        )
        review_only = sum(
            1 for p in plans.values()
            if any(s.severity == "action_required" for s in p.shortcomings)
            and not any(s.blocks_load for s in p.shortcomings)
        )
        migration_summary = {
            "tables": len(plans),
            "chunked": chunked,
            "action_required": action_required,
            "load_blocking": load_blocking,
            "action_required_review_only": review_only,
        }

    result = {
        **provenance(assessment),
        "summary": _to_dict(assessment.summary),
        "cost": _to_dict(assessment.cost),
        "failures": _to_dict(assessment.failures),
        "engine_recommendation": _to_dict(assessment.engine_recommendation),
        "migration_plans": migration_summary,
        "source_db_setup": assessment.source_db_setup,
    }
    return result


def serialize_entities(assessment: Assessment) -> tuple[list[dict], list[dict]]:
    """Serialize all entities once; return (effort_entities, query_entities).

    Avoids calling _to_dict() twice on TABLE entities (which appear in both views).
    The result is memoized on the assessment instance: JSONWriter and HTMLWriter both
    call this in the default ``--format json,html`` run, and the recursive walk over
    every entity (column schemas, view SQL) is the most expensive serialization step
    at large-warehouse scale.
    """
    cached = getattr(assessment, "_serialized_entities", None)
    if cached is not None:
        return cached
    effort: list[dict] = []
    query: list[dict] = []
    # Pre-serialize migration plans so per-entity attachment is a dict lookup
    plans = _to_dict(assessment.migration_plans) if assessment.migration_plans else {}
    for e in assessment.entities:
        d = _to_dict(e)
        # Add physical and logical size for display
        d["logical_size_gb"] = e.size_gb
        pb = e.physical_bytes
        d["physical_size_gb"] = round(pb / (1024 ** 3), 4) if pb is not None else round(e.size_gb * k.ASSUMED_PHYSICAL_RATIO, 4)
        # Attach per-entity Athena migration plan (keyed by full_name in plans dict)
        plan = plans.get(e.full_name)
        if plan:
            d["migration_plan"] = plan
        query.append(d)
        if e.population is EntityPopulation.TABLE:
            effort.append(d)
    assessment._serialized_entities = (effort, query)
    return assessment._serialized_entities


# Fields the HTML report's client-side table renderer uses. Full entity dicts carry
# column schemas and view SQL that the report tables never show — dropping them keeps
# the embedded JSON (and thus the report file) small at warehouse scale. The template's
# JS renderer and these allowlists are pinned together by
# tests/unit/test_report_serialize.py::test_report_rows_cover_template_accesses.
#
# Heavy detail-only fields (conversion DDL, migration DML, translated SQL) are NOT in
# the row allowlists: they ship in separate per-chunk JSON script tags that the browser
# leaves unparsed until a row is expanded (`detail_chunk` is the row's chunk index).
# The main report-data blob must stay JSON.parse-able at page load for 100k+ entities.
_EFFORT_ROW_KEYS = (
    "full_name", "entity_type", "population", "rows",
    "logical_size_gb", "physical_size_gb",
    "effort", "storage_placement", "detail_chunk",
    "detail_omitted",      # heavy payload past the embed budget — panel points at migration/
)
_QUERY_ROW_KEYS = (
    "full_name", "entity_type", "population",
    "complexity", "depends_on", "rewrite_guidance", "placement", "detail_chunk",
    "detail_omitted",      # heavy payload past the embed budget — panel points at migration/
    "has_translated_sql",  # row-level flag; the SQL itself lives in the detail chunk
    "query_workload",      # lightweight summary: {query_count, slot_hours, statement_types, num_shapes}
    "has_workload",        # boolean flag for JS: true if this entity has attributed query logs
    "query_chunk",         # index of the query-sample chunk holding this entity's samples
)

# Heavy per-entity fields moved out of the row payload into lazy detail chunks.
_DETAIL_KEYS = ("conversion", "migration_plan", "translated_sql")

# Entities per detail chunk: ~4 KB/entity of DDL+DML keeps each chunk's one-time
# JSON.parse (on first expand of a row in that chunk) around 2 MB / tens of ms.
DETAIL_CHUNK_SIZE = 500

# Byte budget for ALL embedded heavy detail (chunk JSON, CLAUDE.md § Scale rule).
# Chunks are lazy-parsed but still ship inside the single HTML file — embedding
# DDL/DML for every entity of a petabyte-scale estate makes the file itself
# unloadable (measured 2026-08-14: 100k entities → 383 MB of chunks → 471 MB HTML).
# Entities are embedded in attention order (REBUILT SQL first — views/MVs/routines
# are the review surface — then flagged tables, then by size); entities past the
# budget keep their detail panel but point at migration/plan.json +
# redshift_phase.sql, the canonical runnable copies. Never silent: the count is
# returned and rendered in the report toolbar.
DETAIL_BYTE_BUDGET = 64 * 1024 * 1024


def _detail_priority(d: dict) -> tuple:
    """Attention-first embed order. Lower sorts first."""
    if d.get("population") == "REBUILT":
        tier = 0                                   # SQL definitions: the review surface
    elif (d.get("effort") or {}).get("flags"):
        tier = 1                                   # tables someone must look at
    else:
        tier = 2
    return (tier, -(d.get("logical_size_gb") or 0.0))

# Nested fields the renderer never reads. `complexity.constructs` matters most: it
# carries per-construct anonymized SQL snippets, which at query-log scale dominate
# the payload (and would ship SQL text the report never displays).
_NESTED_DROP_KEYS = {
    "complexity": ("constructs",),
    "placement": ("refresh_unverified",),
    "conversion": ("success",),
    # DDL + load statements live in migration/redshift_phase.sql (the canonical,
    # runnable copy) — the report renders only target + signals.
    "storage_placement": ("redshift_ddl", "redshift_load"),
}


def _project_row(d: dict, keys: tuple[str, ...]) -> dict:
    """Copy the allowlisted keys, pruning nested dead fields without mutating ``d``.

    ``d`` is shared with the JSON sidecar writer, so nested dicts are shallow-copied
    rather than edited in place.
    """
    row = {}
    for key in keys:
        value = d.get(key)
        if value is None:
            continue
        drop = _NESTED_DROP_KEYS.get(key)
        if drop and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in drop}
        row[key] = value
    return row


def build_report_rows(
    effort_entities: list[dict], query_entities: list[dict],
    detail_byte_budget: int = DETAIL_BYTE_BUDGET,
) -> tuple[dict, list[dict], int]:
    """Project serialized entity dicts down to the HTML report's table payload.

    Rows without a score are dropped here, mirroring the former template-side
    ``{% if e.effort %}`` / ``{% if e.complexity %}`` guards.

    Returns ``(rows, detail_chunks, detail_omitted_count)``. Heavy fields
    (Iceberg DDL, load DML, translated SQL) are split into ``detail_chunks`` —
    lists of ``{full_name: {conversion, migration_plan, translated_sql}}`` dicts
    of ``DETAIL_CHUNK_SIZE`` entities each. The template embeds each chunk as its
    own inert JSON script tag; the JS parses a chunk only when a row in it is
    first expanded. Keeping them out of ``rows`` keeps the page-load
    ``JSON.parse`` proportional to entity count, not DDL volume.

    Embedded detail is capped at ``detail_byte_budget`` JSON bytes in
    ``_detail_priority`` order (CLAUDE.md § Scale rule); past the budget the row
    gets ``detail_omitted: true`` and its panel points at migration/plan.json.
    """
    # Heavy payloads first, in attention order, until the byte budget is spent.
    heavy_of: dict[str, dict] = {}
    omitted: set[str] = set()
    budget_left = detail_byte_budget
    for d in sorted(query_entities, key=_detail_priority):  # superset of effort
        heavy = {}
        for key in _DETAIL_KEYS:
            value = d.get(key)
            if value is None:
                continue
            drop = _NESTED_DROP_KEYS.get(key)
            if drop and isinstance(value, dict):
                value = {k: v for k, v in value.items() if k not in drop}
            heavy[key] = value
        if not heavy:
            continue
        cost = len(json.dumps(heavy, separators=(",", ":")))
        if cost > budget_left:
            omitted.add(d["full_name"])
            continue
        budget_left -= cost
        heavy_of[d["full_name"]] = heavy

    # Chunk in ROW order so a page of rows touches few chunks, not one per row.
    detail_chunks: list[dict] = []
    chunk_of: dict[str, int] = {}
    current: dict[str, dict] = {}
    for d in query_entities:
        heavy = heavy_of.get(d["full_name"])
        if heavy is None:
            continue
        if len(current) >= DETAIL_CHUNK_SIZE:
            detail_chunks.append(current)
            current = {}
        current[d["full_name"]] = heavy
        chunk_of[d["full_name"]] = len(detail_chunks)
    if current:
        detail_chunks.append(current)

    def _effort_row(d: dict) -> dict:
        row = _project_row(d, _EFFORT_ROW_KEYS)
        chunk = chunk_of.get(d["full_name"])
        if chunk is not None:
            row["detail_chunk"] = chunk
        elif d["full_name"] in omitted:
            row["detail_omitted"] = True
        return row

    def _query_row(d: dict) -> dict:
        row = _project_row(d, _QUERY_ROW_KEYS)
        chunk = chunk_of.get(d["full_name"])
        if chunk is not None:
            row["detail_chunk"] = chunk
        elif d["full_name"] in omitted:
            row["detail_omitted"] = True
        if d.get("translated_sql") is not None:
            row["has_translated_sql"] = True
        return row

    rows = {
        "effort": [
            _effort_row(d) for d in effort_entities if d.get("effort")
        ],
        "query": [
            _query_row(d) for d in query_entities if d.get("complexity")
        ],
    }
    return rows, detail_chunks, len(omitted)


QUERY_SAMPLE_CHUNK_SIZE = 100

# Per-sample SQL clip for the HTML embed. With no per-estate entity cap
# (2026-08-04), report size is bounded per SAMPLE instead: 3 shapes/entity ×
# ≤2×4 KB each ≈ 24 KB worst case per entity, dominated in practice by short
# production queries. The untruncated SQL is always in query-workload/.
MAX_SAMPLE_SQL_CHARS = 4_000
_CLIP_NOTE = "\n-- … truncated for the report; full query in query-workload/"


def _clip_sql(sql: str) -> str:
    if len(sql) <= MAX_SAMPLE_SQL_CHARS:
        return sql
    return sql[:MAX_SAMPLE_SQL_CHARS] + _CLIP_NOTE


# Byte budget for embedded query samples (CLAUDE.md § Scale rule). Per-sample
# clipping bounds each entity (~24 KB worst) but not the estate: 100k attributed
# entities would ship GBs. Heaviest workloads (by slot-ms) keep their SQL; the
# rest keep their workload stats on the row, sample-less. Never silent — the
# omitted count is returned and rendered in the report.
QUERY_SAMPLE_BYTE_BUDGET = 32 * 1024 * 1024


def build_query_sample_chunks(
    query_workloads: dict[str, dict],
    sample_byte_budget: int = QUERY_SAMPLE_BYTE_BUDGET,
) -> tuple[list[dict], dict[str, int], int]:
    """Build chunked query-sample JSON for lazy loading in the report.

    Each chunk is a dict of {entity_full_name: [{o, t, s, m}, ...]} holding
    the top-N query samples per entity, SQL clipped at MAX_SAMPLE_SQL_CHARS.
    Chunks are capped at QUERY_SAMPLE_CHUNK_SIZE entities so each one-time
    JSON.parse (on first expand of a row in that chunk) stays small.

    Returns ``(chunks, entity_to_chunk, omitted_count)``: the index map lets
    the JS parse ONLY the chunk holding the expanded entity. Without it,
    getQuerySamples walked and parsed EVERY chunk until it found the entity —
    one expand could parse the entire sample payload (~26 MB at 4,247
    entities), blocking the UI exactly on the large estates chunking exists
    for (2026-08-04 review). Estate-level size is bounded by
    ``sample_byte_budget``, spent heaviest-workload-first.

    The query_workloads dict maps entity full_name → {samples: [{query, translated, statement_type, total_slot_ms}]}.
    """
    def _weight(item: tuple[str, dict]) -> int:
        return -max(
            (s.get("total_slot_ms") or 0) for s in item[1].get("samples", [{}])
        )

    chunks: list[dict] = []
    entity_to_chunk: dict[str, int] = {}
    current: dict = {}
    omitted = 0
    budget_left = sample_byte_budget
    for entity_name, wl in sorted(query_workloads.items(), key=_weight):
        samples = wl.get("samples", [])
        if not samples:
            continue
        entry = [
            {
                "o": _clip_sql(s["query"]),
                "t": _clip_sql(s["translated"]),
                "s": s["statement_type"],
                "m": s["total_slot_ms"],
            }
            for s in samples
        ]
        cost = len(json.dumps(entry, separators=(",", ":")))
        if cost > budget_left:
            omitted += 1
            continue
        budget_left -= cost
        current[entity_name] = entry
        entity_to_chunk[entity_name] = len(chunks)
        if len(current) >= QUERY_SAMPLE_CHUNK_SIZE:
            chunks.append(current)
            current = {}
    if current:
        chunks.append(current)
    return chunks, entity_to_chunk, omitted


