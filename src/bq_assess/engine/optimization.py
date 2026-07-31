"""Post-migration optimization steps for S3 Tables targets.

S3 Tables runs compaction and snapshot management as MANAGED maintenance
(enabled by default; Athena's OPTIMIZE/VACUUM are unsupported on the
s3tablescatalog and self-managed rewrites can conflict with the managed
compactor). The only remaining per-table step is a one-time sort-order
declaration: BigQuery clustering has no Athena-DDL equivalent, but once
Spark sets the Iceberg sort order in table metadata, S3 Tables' default
`auto` compaction strategy applies sort compaction continuously.
(Verified against the S3 Tables maintenance docs, 2026-07-31.)
"""
from __future__ import annotations

from collections.abc import Sequence

from bq_assess.models import EngineConfig, EntityMetadata, PostMigrationStep

_GB = 1024**3


def iceberg_table_name(full_name: str) -> str:
    """Convert BQ full_name (dataset.table) to Iceberg table identifier.

    Uses the dataset as the namespace and the table as the table name,
    matching the DDL convention (CREATE TABLE dataset.table_name).
    """
    parts = full_name.split(".", 1)
    if len(parts) == 2:
        return f"{parts[0]}.{parts[1]}"
    return full_name


def spark_sort_command(table: str, sort_cols: Sequence[str]) -> str:
    """One-time Spark DDL that persists the sort order in Iceberg metadata.

    S3 Tables' managed compaction reads the sort order from table metadata
    (auto strategy -> sort compaction) — no recurring rewrite_data_files runs
    are needed, and running them would race the managed compactor.
    """
    order = ", ".join(f"{col} ASC NULLS LAST" for col in sort_cols)
    return f"ALTER TABLE {table} WRITE ORDERED BY ({order})"


def generate_post_optimization(
    entity: EntityMetadata, config: EngineConfig
) -> list[PostMigrationStep]:
    steps: list[PostMigrationStep] = []
    table = entity.full_name

    # Sort order (when BQ has clustering): one-time metadata change via Spark
    # (EMR/Glue — Athena rejects custom TBLPROPERTIES so it cannot set this).
    if entity.clustering_fields:
        table_iceberg = iceberg_table_name(table)
        size_gb = entity.num_bytes / _GB
        priority = "recommended" if size_gb > config.compaction_threshold_gb else "optional"
        steps.append(PostMigrationStep(
            table=table,
            step_type="sort",
            command=spark_sort_command(table_iceberg, entity.clustering_fields),
            engine="spark_emr",
            reason=(
                f"BQ clustering on [{', '.join(entity.clustering_fields)}] has no "
                "Athena equivalent — set the Iceberg sort order once and S3 Tables "
                "managed compaction keeps data sorted from then on"
            ),
            priority=priority,
        ))

    return steps
