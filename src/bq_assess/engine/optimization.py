"""Post-migration optimization steps — sort, compact, partition evolution.

Emitted per-table when the BQ source has characteristics that Athena's INSERT
cannot replicate (clustering → sort order, many small files → compaction).
"""
from __future__ import annotations

from typing import Sequence

from bq_assess.models import EngineConfig, EntityMetadata, PostMigrationStep

_GB = 1024**3


def iceberg_table_name(full_name: str) -> str:
    """Convert BQ full_name (dataset.table) to Iceberg table identifier."""
    return f"iceberg_db.{full_name.replace('.', '_')}"


def spark_sort_command(table: str, sort_cols: Sequence[str], catalog: str = "spark_catalog") -> str:
    """Build Iceberg rewrite_data_files CALL for sorting."""
    sort_order = ", ".join(f"{col} ASC NULLS LAST" for col in sort_cols)
    return (
        f"CALL {catalog}.system.rewrite_data_files("
        f"table => '{table}', strategy => 'sort', sort_order => '{sort_order}')"
    )


def generate_post_optimization(
    entity: EntityMetadata, config: EngineConfig
) -> list[PostMigrationStep]:
    steps: list[PostMigrationStep] = []
    table = entity.full_name

    # Sort order (when BQ has clustering)
    if entity.clustering_fields:
        table_iceberg = iceberg_table_name(table)
        size_gb = entity.num_bytes / _GB
        priority = "recommended" if size_gb > config.compaction_threshold_gb else "optional"
        steps.append(PostMigrationStep(
            table=table,
            step_type="sort",
            command=spark_sort_command(table_iceberg, entity.clustering_fields),
            engine="spark_emr",
            reason=f"BQ clustering on [{', '.join(entity.clustering_fields)}] has no Athena equivalent during INSERT",
            priority=priority,
        ))

    # Compaction (always — Athena INSERT creates many small files)
    table_iceberg = iceberg_table_name(table)
    steps.append(PostMigrationStep(
        table=table,
        step_type="compact",
        command=f"OPTIMIZE {table_iceberg} REWRITE DATA USING BIN_PACK",
        engine="athena",
        reason="Post-load compaction reduces small-file overhead from chunked INSERTs",
        priority="recommended",
    ))

    # Vacuum (expire snapshots and remove orphan files after compaction)
    steps.append(PostMigrationStep(
        table=table,
        step_type="vacuum",
        command=f"VACUUM {table_iceberg}",
        engine="athena",
        reason="Expire snapshots and remove orphan files left by DML and compaction (OPTIMIZE does not do this); billed via S3 API requests",
        priority="recommended",
    ))

    return steps
