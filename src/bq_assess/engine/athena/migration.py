"""Athena Migration DML Generator — INSERT...SELECT with shortcoming detection.

Athena is the sole migration/load engine. Generates INSERT statements for loading
data into Iceberg tables, flags shortcomings, and emits post-migration optimization steps.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bq_assess.engine.optimization import (
    generate_post_optimization,
    iceberg_table_name,
    spark_sort_command,
)
from bq_assess.models import (
    ConversionResult,
    EngineConfig,
    EntityMetadata,
    EntityPopulation,
    MigrationDML,
    MigrationShortcoming,
)
from bq_assess.targets.iceberg.identifiers import quote_identifier

_TYPES_NEEDING_CAST = frozenset({
    "GEOGRAPHY", "INTERVAL", "RANGE", "JSON", "BIGNUMERIC", "TIME", "BYTES"
})

_LARGE_TABLE_BYTES = 100 * 1024**3  # 100 GB
# Safety margin under Athena's 100-open-partition write limit
_SAFE_PARTITIONS_PER_INSERT = 90


def _partitions_per_day(granularity: str) -> float:
    """Return how many partitions are created per day for given BQ granularity."""
    g = granularity.upper()
    if g == "HOUR":
        return 24.0
    elif g == "DAY":
        return 1.0
    elif g == "MONTH":
        return 1.0 / 30.0
    elif g == "YEAR":
        return 1.0 / 365.0
    return 1.0  # default to DAY


class AthenaMigrationGenerator:
    """Generate Athena INSERT...SELECT migration DML for tables."""

    def generate(
        self,
        entity: EntityMetadata,
        conversion: ConversionResult,
        config: EngineConfig,
    ) -> MigrationDML:
        if entity.population == EntityPopulation.REBUILT:
            return MigrationDML(
                table=entity.full_name,
                statements=[],
                shortcomings=[],
                post_optimization=[],
                estimated_scan_bytes=None,
            )

        shortcomings = self._detect_shortcomings(entity, config)
        statements = self._generate_statements(entity, conversion, config)
        post_opt = (
            generate_post_optimization(entity, config)
            if config.post_optimization else []
        )

        return MigrationDML(
            table=entity.full_name,
            statements=statements,
            shortcomings=shortcomings,
            post_optimization=post_opt,
            estimated_scan_bytes=entity.num_bytes,
        )

    def _generate_statements(
        self,
        entity: EntityMetadata,
        conversion: ConversionResult,
        config: EngineConfig,
    ) -> list[str]:
        target = iceberg_table_name(entity.full_name)
        source = f"source_db.{entity.full_name.replace('.', '_')}"

        # Athena fails partitioned Iceberg INSERTs at >100 open partitions,
        # regardless of byte size; large tables also chunk for retry safety
        is_partitioned = entity.time_partitioning is not None
        is_large = entity.num_bytes > _LARGE_TABLE_BYTES
        estimated_partitions = self._estimate_partition_count(entity)

        needs_chunking = is_partitioned and (estimated_partitions > 100 or is_large)

        if needs_chunking:
            return self._chunked_insert(target, source, entity, config)

        return [self._simple_insert(target, source, entity)]

    def _build_select_clause(self, entity: EntityMetadata) -> str:
        """Build SELECT clause with casts for special types, or * if none needed.

        Column names that are reserved words are double-quoted via quote_identifier (Fix 2).
        """
        if not entity.columns:
            return "*"

        # Check if any columns need casting
        cast_cols = {
            col.name: col.field_type.upper()
            for col in entity.columns
            if col.field_type.upper() in _TYPES_NEEDING_CAST
        }

        if not cast_cols:
            return "*"

        # Build explicit column list with casts — quote all identifiers when explicit
        select_items = []
        for col in entity.columns:
            col_type = col.field_type.upper()
            quoted = quote_identifier(col.name)
            if col.name not in cast_cols:
                # Normal column, pass through (quoted if reserved)
                select_items.append(quoted)
            elif col_type == "JSON":
                select_items.append(f"CAST({quoted} AS varchar) -- JSON -> varchar")
            elif col_type == "GEOGRAPHY":
                select_items.append(f"CAST({quoted} AS varchar) /* WKT */")
            elif col_type == "BIGNUMERIC":
                select_items.append(
                    f"try_cast({quoted} AS decimal(38,9)) /* BIGNUMERIC: out-of-range values become NULL */"
                )
            elif col_type == "TIME":
                select_items.append(
                    f"CAST({quoted} AS varchar) -- Athena cannot write Iceberg TIME"
                )
            elif col_type in ("INTERVAL", "RANGE"):
                select_items.append(f"CAST({quoted} AS varchar)")
            elif col_type == "BYTES":
                # BYTES maps to Iceberg string (verified in converter LOSSY_TYPE_MAP)
                # Must be encoded (e.g., TO_BASE64) before load
                select_items.append(f"CAST({quoted} AS varchar) -- BYTES -> base64 encoding required")
            else:
                # Fallback: pass through
                select_items.append(quoted)

        return ",\n    ".join(select_items)

    def _simple_insert(
        self,
        target: str,
        source: str,
        entity: EntityMetadata | None = None,
    ) -> str:
        select_clause = self._build_select_clause(entity) if entity else "*"
        return (
            f"-- PREREQUISITES: (1) source_db external tables must exist in Glue over the transferred data; (2) run in a workgroup with Athena engine v3; (3) statements assume database context via fully-qualified names.\n"
            f"-- Athena INSERT...SELECT (full table load)\n"
            f"INSERT INTO {target}\n"
            f"SELECT {select_clause} FROM {source};"
        )

    def _chunked_insert(
        self,
        target: str,
        source: str,
        entity: EntityMetadata,
        config: EngineConfig,
    ) -> list[str]:
        # Check for ingestion-time partitioning (partition exists but field is None)
        if entity.time_partitioning and entity.time_partitioning.field is None:
            # No queryable partition column — emit template with warning
            select_clause = self._build_select_clause(entity)
            return [
                f"-- PREREQUISITES: (1) source_db external tables must exist in Glue over the transferred data; (2) run in a workgroup with Athena engine v3; (3) statements assume database context via fully-qualified names.\n"
                f"-- TEMPLATE: Athena INSERT...SELECT (chunked by {config.chunk_days}-day windows)\n"
                f"-- WARNING: Source uses ingestion-time partitioning (_PARTITIONTIME); "
                f"substitute the real ingestion-time column or _ingestion_time surrogate before running\n"
                f"-- Run this table's chunks SEQUENTIALLY (Iceberg optimistic locking — concurrent writes to one table can conflict).\n"
                f"-- Parallelize across DIFFERENT tables, up to the account's active-DML quota (100 in ap-southeast-2, 200 in us-east-1; adjustable)\n"
                f"-- Each chunk is idempotent — the DELETE clears any partial prior attempt; safe to re-run\n"
                f"-- Each DML statement must finish within the Athena DML timeout (default 30 min, adjustable to 240) — split windows further if a chunk approaches it\n"
                f"DELETE FROM {target} WHERE {{{{partition_field}}}} >= DATE '{{{{start}}}}' AND {{{{partition_field}}}} < DATE '{{{{end}}}}';\n"
                f"INSERT INTO {target}\n"
                f"SELECT {select_clause} FROM {source}\n"
                f"WHERE {{{{partition_field}}}} >= DATE '{{{{start}}}}' AND {{{{partition_field}}}} < DATE '{{{{end}}}}';\n"
                f"-- Repeat for each {config.chunk_days}-day window across the partition range"
            ]

        raw_field = entity.time_partitioning.field if entity.time_partitioning else "partition_col"
        # Quote the partition field if it is a reserved word (Fix 2)
        field = quote_identifier(raw_field)
        chunk_days = config.chunk_days
        select_clause = self._build_select_clause(entity)

        # Emit concrete per-window statements so the deliverable is executable
        chunks = self._generate_chunk_windows(entity, chunk_days)

        if not chunks:
            # No date range available → emit template with clear marker
            return [
                f"-- PREREQUISITES: (1) source_db external tables must exist in Glue over the transferred data; (2) run in a workgroup with Athena engine v3; (3) statements assume database context via fully-qualified names.\n"
                f"-- TEMPLATE: Athena INSERT...SELECT (chunked by {chunk_days}-day windows on {raw_field})\n"
                f"-- WARNING: No creation date available; substitute concrete dates before execution\n"
                f"-- Run this table's chunks SEQUENTIALLY (Iceberg optimistic locking — concurrent writes to one table can conflict).\n"
                f"-- Parallelize across DIFFERENT tables, up to the account's active-DML quota (100 in ap-southeast-2, 200 in us-east-1; adjustable)\n"
                f"-- Each chunk is idempotent — the DELETE clears any partial prior attempt; safe to re-run\n"
                f"-- Each DML statement must finish within the Athena DML timeout (default 30 min, adjustable to 240) — split windows further if a chunk approaches it\n"
                f"DELETE FROM {target} WHERE {field} >= DATE '{{{{start}}}}' AND {field} < DATE '{{{{end}}}}';\n"
                f"INSERT INTO {target}\n"
                f"SELECT {select_clause} FROM {source}\n"
                f"WHERE {field} >= DATE '{{{{start}}}}' AND {field} < DATE '{{{{end}}}}';\n"
                f"-- Repeat for each {chunk_days}-day window across the partition range"
            ]

        statements = [
            f"-- PREREQUISITES: (1) source_db external tables must exist in Glue over the transferred data; (2) run in a workgroup with Athena engine v3; (3) statements assume database context via fully-qualified names.\n"
            f"-- STEP 0: verify the actual data range before running chunks (window bounds below derive from table metadata dates)\n"
            f"SELECT MIN({field}) AS min_val, MAX({field}) AS max_val FROM {source};\n",
            f"-- Athena INSERT...SELECT (chunked by {chunk_days}-day windows on {raw_field})\n"
            f"-- Windows derived from table metadata dates; extend/trim after STEP 0\n"
            f"-- Run this table's chunks SEQUENTIALLY (Iceberg optimistic locking — concurrent writes to one table can conflict).\n"
            f"-- Parallelize across DIFFERENT tables, up to the account's active-DML quota (100 in ap-southeast-2, 200 in us-east-1; adjustable)\n"
            f"-- Each chunk is idempotent — the DELETE clears any partial prior attempt; safe to re-run\n"
            f"-- Each DML statement must finish within the Athena DML timeout (default 30 min, adjustable to 240) — split windows further if a chunk approaches it\n"
        ]

        # Emit up to first 5 chunk pairs fully, then summarize remainder
        for i, (start, end) in enumerate(chunks[:5]):
            statements.append(
                f"DELETE FROM {target} WHERE {field} >= DATE '{start}' AND {field} < DATE '{end}';\n"
                f"INSERT INTO {target}\n"
                f"SELECT {select_clause} FROM {source}\n"
                f"WHERE {field} >= DATE '{start}' AND {field} < DATE '{end}';\n"
            )

        if len(chunks) > 5:
            statements.append(
                f"\n-- ... plus {len(chunks) - 5} more chunks. Remaining windows:\n"
            )
            for start, end in chunks[5:]:
                statements.append(f"-- {start} to {end}\n")

        return statements

    def _estimate_partition_count(self, entity: EntityMetadata) -> int:
        """Estimate partition count for the chunking decision."""
        if not entity.time_partitioning:
            # Range partitioning
            if entity.range_partitioning:
                rp = entity.range_partitioning
                if rp.interval > 0:
                    return (rp.end - rp.start) // rp.interval
            return 0

        # Use creation date → now as the range
        if not entity.last_modified:
            return 0

        now = datetime.now(timezone.utc)
        # Treat last_modified as a proxy for creation date (conservative estimate)
        creation_date = entity.last_modified
        days = (now - creation_date).days

        granularity = entity.time_partitioning.type
        ppd = _partitions_per_day(granularity)
        return int(days * ppd)

    def _generate_chunk_windows(
        self,
        entity: EntityMetadata,
        chunk_days: int,
    ) -> list[tuple[str, str]]:
        """Generate concrete (start, end) date pairs for chunked INSERT.

        Scales window size based on partition granularity to avoid exceeding
        Athena's 100-open-partition write limit (using 90 as safety margin).
        """
        if not entity.last_modified:
            return []

        # Scale chunk_days based on partition granularity
        effective_chunk_days = chunk_days
        if entity.time_partitioning:
            granularity = entity.time_partitioning.type
            ppd = _partitions_per_day(granularity)
            # Derive window size so partitions stay under safety limit
            # e.g., HOUR (24 ppd) → 90/24 = 3.75 → 3 days
            effective_chunk_days = max(1, int(_SAFE_PARTITIONS_PER_INSERT / ppd))
            # Cap by config limit
            effective_chunk_days = min(effective_chunk_days, chunk_days)

        # Floor at 1 to guard against chunk_days=0 hanging the loop
        effective_chunk_days = max(1, effective_chunk_days)

        now = datetime.now(timezone.utc)
        start_date = entity.last_modified.date()
        end_date = now.date()

        chunks: list[tuple[str, str]] = []
        current = start_date
        while current < end_date:
            chunk_end = min(current + timedelta(days=effective_chunk_days), end_date)
            chunks.append((str(current), str(chunk_end)))
            current = chunk_end

        return chunks

    def _detect_shortcomings(self, entity: EntityMetadata, config: EngineConfig) -> list[MigrationShortcoming]:
        shortcomings: list[MigrationShortcoming] = []

        # Sort order gap
        if entity.clustering_fields:
            cols = ", ".join(entity.clustering_fields)
            table_iceberg = iceberg_table_name(entity.full_name)
            shortcomings.append(MigrationShortcoming(
                category="sort_order",
                severity="advisory",
                bq_source=f"clustering_fields: [{cols}]",
                description="Athena INSERT preserves no sort order — scan efficiency degrades without sort",
                remediation=f"EMR Spark: {spark_sort_command(table_iceberg, entity.clustering_fields)}",
                remediation_engine="spark",
            ))

        # Type cast gap
        cast_cols = [
            col.name for col in entity.columns
            if col.field_type.upper() in _TYPES_NEEDING_CAST
        ]
        if cast_cols:
            bignumeric_cols = [
                col.name for col in entity.columns
                if col.field_type.upper() == "BIGNUMERIC"
            ]
            base_desc = f"Columns {cast_cols} use types requiring CAST (emitted in generated SQL)"
            if bignumeric_cols:
                base_desc += ". BIGNUMERIC exceeds Athena DECIMAL(38) — out-of-range values become NULL via try_cast"

            base_remediation = "Review emitted CAST expressions; BYTES columns require base64 encoding before load"
            if bignumeric_cols:
                base_remediation += f". For BIGNUMERIC columns {bignumeric_cols}: audit pre-migration with WHERE col IS NOT NULL AND try_cast(col AS decimal(38,9)) IS NULL, or cast to varchar for full fidelity"

            shortcomings.append(MigrationShortcoming(
                category="type_cast",
                severity="action_required",
                bq_source=f"columns: {cast_cols}",
                description=base_desc,
                remediation=base_remediation,
                remediation_engine="manual",
            ))

        # Partition evolution gap (if partition spec might need changing post-migration)
        if entity.time_partitioning and entity.time_partitioning.field is None:
            shortcomings.append(MigrationShortcoming(
                category="partition_evolution",
                severity="advisory",
                bq_source="ingestion-time partitioning (_PARTITIONTIME)",
                description="Athena cannot ALTER TABLE SET PARTITION SPEC post-creation",
                remediation="Define partition spec at table creation; changes require re-create or EMR Spark ALTER",
                remediation_engine="spark",
            ))

        # Compaction advisory for large tables
        threshold_bytes = config.compaction_threshold_gb * (1024 ** 3)
        if entity.num_bytes > threshold_bytes:
            table_iceberg = iceberg_table_name(entity.full_name)
            shortcomings.append(MigrationShortcoming(
                category="compaction",
                severity="advisory",
                bq_source=f"table size: {entity.num_bytes / (1024**3):.2f} GB",
                description=f"Table exceeds {config.compaction_threshold_gb:.1f} GB threshold; post-load compaction recommended",
                remediation=f"OPTIMIZE {table_iceberg} REWRITE DATA USING BIN_PACK",
                remediation_engine="athena",
            ))

        return shortcomings
