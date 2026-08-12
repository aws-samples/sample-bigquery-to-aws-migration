"""Query-workload sidecar: every attributed query shape, translated, on disk.

The HTML report embeds only the top 5 query shapes per entity (by slot cost) —
that cap keeps the single-file report bounded at petabyte-scale estates. The
rest of the shapes were previously reachable only by grepping the bundle's
queries.jsonl. This writer gives them a consumable home: one .sql file per
entity under query-workload/, each shape as original BigQuery SQL followed by
its translation to the recommended engine, ordered by slot cost.

Layout:
    query-workload/
        INDEX.csv                     # entity, queries, slot_hours, shapes, file
        <dataset>.<table>.sql         # all shapes, original + translated
"""
from __future__ import annotations

import csv
import os
import re

from bq_assess.core.query_attribution import EntityWorkload
from bq_assess.core.query_translator import translate_query

_ENGINE_LABELS = {"redshift": "Redshift", "athena": "Athena"}

# Entity full_names are dataset.table (BQ identifier charset) — already safe as
# filenames. The sub is belt-and-suspenders for exotic identifiers.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._$-]")


def _filename(entity_name: str) -> str:
    return _UNSAFE_RE.sub("_", entity_name) + ".sql"


def write_workload_sidecar(
    query_workload_map: dict[str, EntityWorkload],
    target_engine: str,
    project_dir: str,
) -> str | None:
    """Write the per-entity workload files; returns the directory (None if empty).

    Every distinct query shape is written — including the top 5 the report
    embeds, so each file is complete on its own. Translation reuses a
    text-level cache: a query shape attributed to N entities translates once.
    """
    entities = {
        name: wl for name, wl in query_workload_map.items() if wl.all_shapes
    }
    if not entities:
        return None

    engine_label = _ENGINE_LABELS.get(target_engine, target_engine)
    out_dir = os.path.join(project_dir, "query-workload")
    os.makedirs(out_dir, exist_ok=True)

    translation_cache: dict[str, str] = {}
    index_rows = []

    for entity_name in sorted(entities):
        wl = entities[entity_name]
        fname = _filename(entity_name)
        path = os.path.join(out_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                f"-- Production query workload: {entity_name}\n"
                f"-- {wl.query_count} queries, {wl.slot_hours} slot-hours, "
                f"{wl.num_shapes} distinct statements (all listed, heaviest first)\n"
                f"-- Source: INFORMATION_SCHEMA.JOBS (anonymized). "
                f"Translations target {engine_label} — best-effort dialect "
                f"rewrite for scoping; validate before use.\n"
            )
            for i, s in enumerate(wl.all_shapes, 1):
                slot_hrs = s.total_slot_ms / 3_600_000
                translated = translation_cache.get(s.query)
                if translated is None:
                    translated = translate_query(s.query, engine=target_engine)
                    translation_cache[s.query] = translated
                f.write(
                    f"\n-- ============================================================\n"
                    f"-- Statement {i}/{wl.num_shapes} · {s.statement_type} · "
                    f"{slot_hrs:.1f} slot-hrs\n"
                    f"-- ------------------------------ BigQuery (original)\n"
                )
                f.write(_as_comment_block(s.query))
                f.write(
                    f"-- ------------------------------ {engine_label} (translated)\n"
                )
                body = translated.rstrip()
                f.write(body + "\n")
                if not body.endswith(";"):
                    f.write(";\n")
        index_rows.append({
            "entity": entity_name,
            "queries": wl.query_count,
            "slot_hours": wl.slot_hours,
            "distinct_statements": wl.num_shapes,
            "file": fname,
        })

    index_path = os.path.join(out_dir, "INDEX.csv")
    with open(index_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["entity", "queries", "slot_hours", "distinct_statements", "file"]
        )
        writer.writeheader()
        writer.writerows(index_rows)

    return out_dir


def _as_comment_block(sql: str) -> str:
    """Render the original BQ query as SQL comments so each file stays runnable
    against the target engine (only translated statements are live)."""
    lines = sql.rstrip().splitlines() or [""]
    return "".join(f"-- {ln}\n" for ln in lines)
