"""Athena Migration DML Generator — INSERT...SELECT with shortcoming detection.

Athena is the sole migration/load engine. Generates INSERT statements for loading
data into Iceberg tables, flags shortcomings, and emits post-migration optimization steps.
Also generates the Glue federated database setup DDL that the INSERT statements depend on.
"""
from __future__ import annotations

from collections.abc import Sequence
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
from bq_assess.targets.iceberg.identifiers import quote_full_name, quote_identifier


def generate_source_db_setup(
    dataset_id: str,
    gcp_project: str,
    tables: Sequence[EntityMetadata],
    target_region: str = "ap-southeast-2",
) -> tuple[list[str], str]:
    """Generate Glue federated database + connection setup for the BigQuery source.

    The database name matches the BQ dataset exactly, so INSERT statements
    can reference `<dataset>.table_name` — zero naming translation needed.

    Returns a list of SQL/CLI statements (ordered) that the user runs once
    before executing any INSERT...SELECT migration statements.
    """
    statements: list[str] = []

    statements.append(
        f"-- ═══════════════════════════════════════════════════════════════════\n"
        f"-- SOURCE DATABASE SETUP (run once before any migration INSERTs)\n"
        f"-- ═══════════════════════════════════════════════════════════════════\n"
        f"--\n"
        f"-- This creates a Glue federated database backed by the BigQuery Connector.\n"
        f"-- After setup, Athena can query BQ tables as: {dataset_id}.<table_name>\n"
        f"--\n"
        f"-- PREREQUISITES:\n"
        f"--   1. Deploy the Athena BigQuery Connector from AWS Serverless Application Repository\n"
        f"--      (search 'AthenaBigQueryConnector' in the SAR console, deploy to {target_region})\n"
        f"--   2. The connector Lambda needs a GCP service account key with BigQuery read access\n"
        f"--      (roles: BigQuery Data Viewer + Job User + Read Session User — the connector\n"
        f"--      uses the Storage Read API, which needs bigquery.readsessions.create)\n"
        f"--      stored in AWS Secrets Manager; the secret NAME goes in the connector's\n"
        f"--      secret_manager_gcp_creds_name environment variable\n"
        f"--   3. The connector catalog name below must match the Lambda function name\n"
        f"-- ═══════════════════════════════════════════════════════════════════\n"
    )

    # Step 1: Create the Athena data source (federated catalog)
    from bq_assess.engine.athena.naming import connector_name as _connector_name
    connector_name = _connector_name(dataset_id)
    statements.append(
        f"-- STEP 1: Create the Athena federated data source (if not already created)\n"
        f"-- Run via AWS CLI or CloudFormation — Athena DDL cannot create data sources:\n"
        f"--\n"
        f"--   aws athena create-data-catalog \\\n"
        f"--     --name \"{connector_name}\" \\\n"
        f"--     --type LAMBDA \\\n"
        f"--     --parameters function=arn:aws:lambda:{target_region}:{{{{ACCOUNT_ID}}}}:function:{connector_name} \\\n"
        f"--     --region {target_region}\n"
    )

    # Step 2: The database in the connector maps 1:1 to the BQ dataset
    statements.append(
        f"-- STEP 2: Verify the connector exposes the BQ dataset as a database.\n"
        f"-- The BigQuery connector auto-maps GCP projects as catalogs and datasets as databases.\n"
        f"-- After deploying the connector, run this to confirm:\n"
        f"--\n"
        f"SHOW DATABASES IN \"{connector_name}\";\n"
        f"-- Expected: '{dataset_id}' appears in the list\n"
    )

    # Step 3: Verify tables are visible
    table_names = [e.full_name.split(".")[-1] for e in tables[:5]]
    statements.append(
        f"-- STEP 3: Verify tables are accessible through the connector\n"
        f"--\n"
        f"SHOW TABLES IN \"{connector_name}\".{quote_identifier(dataset_id)};\n"
        f"-- Expected tables include: {', '.join(table_names)}"
        + (f" (+ {len(tables) - 5} more)" if len(tables) > 5 else "") + "\n"
        f"--\n"
        f"-- Quick validation query (spot-check one table):\n"
        f"SELECT COUNT(*) FROM \"{connector_name}\".{quote_identifier(dataset_id)}.{quote_identifier(table_names[0])};\n"
    )

    # Step 4: Note on how INSERT references work
    statements.append(
        f"-- ═══════════════════════════════════════════════════════════════════\n"
        f"-- USAGE: All INSERT statements below reference the source as:\n"
        f"--   \"{connector_name}\".{dataset_id}.<table_name>\n"
        f"--\n"
        f"-- The Iceberg target tables live in the S3 Tables federated catalog\n"
        f"-- (s3tablescatalog/<table-bucket>); run_migration.py sets it as the\n"
        f"-- query execution context, so target names stay dataset.table.\n"
        f"-- ═══════════════════════════════════════════════════════════════════\n"
    )

    return statements, connector_name

_TYPES_NEEDING_CAST = frozenset({
    # RANGE is deliberately absent: it's in _CONNECTOR_UNREADABLE — the
    # federated SELECT fails before any CAST could run, so emitting one
    # (and telling the customer to review it) was contradictory advice.
    "GEOGRAPHY", "INTERVAL", "JSON", "BIGNUMERIC", "TIME", "BYTES",
    # Live-verified against the BigQuery connector (2025.41.1, 2026-07-30):
    # TIMESTAMP arrives as varchar (ISO-8601 'T' format) and DATETIME as
    # timestamp(3) — both need explicit casts to land in Iceberg timestamp(6).
    "TIMESTAMP", "DATETIME",
})

# Column types the federated BigQuery connector cannot read at all
# (live-verified 2026-07-30: arrays fail with "Lists have one child Field.
# Found: none"; structs containing timestamps throw IllegalArgumentException;
# RANGE fails with "Unsupported 'Complex' vector VarCharVector").
_CONNECTOR_UNREADABLE = ("RECORD", "STRUCT", "RANGE")

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

    def __init__(self, connector_name: str | None = None):
        self._connector_name = connector_name

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

        # Post-load validation: one query comparing federated-source vs Iceberg
        # counts. NOTE: the source COUNT(*) federates to BigQuery and bills a
        # BQ scan — run after the load, not per-chunk.
        target = quote_full_name(iceberg_table_name(entity.full_name))
        source = self._source_ref(entity)
        validation_query = (
            f"-- Post-load row-count validation for {entity.full_name} "
            f"(source COUNT federates to BigQuery — run once, after the load)\n"
            f"SELECT src.n AS source_rows, tgt.n AS target_rows, "
            f"src.n - tgt.n AS missing\n"
            f"FROM (SELECT COUNT(*) AS n FROM {source}) src\n"
            f"CROSS JOIN (SELECT COUNT(*) AS n FROM {target}) tgt;"
        )

        return MigrationDML(
            table=entity.full_name,
            statements=statements,
            shortcomings=shortcomings,
            post_optimization=post_opt,
            estimated_scan_bytes=entity.num_bytes,
            validation_query=validation_query,
        )

    def _source_ref(self, entity: EntityMetadata) -> str:
        """Build the fully-qualified source table reference via the federated connector.

        Each part is quoted when required (quote_identifier) — BigQuery allows
        hyphens and leading digits in table names, which are invalid unquoted
        Trino identifiers (the 2026-07-31 sandbox validation found 310 DELETE/
        INSERT statements failing to parse on hyphenated names).
        """
        table_name = quote_identifier(entity.full_name.split(".")[-1])
        dataset = quote_identifier(entity.dataset_id)
        if self._connector_name:
            return f'"{self._connector_name}".{dataset}.{table_name}'
        return f"{dataset}.{table_name}"

    def _generate_statements(
        self,
        entity: EntityMetadata,
        conversion: ConversionResult,
        config: EngineConfig,
    ) -> list[str]:
        # Quote each part for Trino DML — hyphens/leading digits in BQ names are
        # invalid unquoted (same 2026-07-31 fix as _source_ref).
        target = quote_full_name(iceberg_table_name(entity.full_name))
        source = self._source_ref(entity)

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
            # Per-column annotations must be block comments: items are joined with
            # ",\n" so a trailing `-- comment` would swallow the list comma.
            elif col_type == "JSON":
                select_items.append(f"CAST({quoted} AS varchar) /* JSON -> varchar */")
            elif col_type == "GEOGRAPHY":
                select_items.append(f"CAST({quoted} AS varchar) /* WKT */")
            elif col_type == "BIGNUMERIC":
                select_items.append(
                    f"try_cast({quoted} AS decimal(38,9)) /* BIGNUMERIC: out-of-range values become NULL */"
                )
            elif col_type == "TIME":
                select_items.append(
                    f"CAST({quoted} AS varchar) /* Athena cannot write Iceberg TIME */"
                )
            elif col_type == "INTERVAL":
                select_items.append(f"CAST({quoted} AS varchar)")
            elif col_type == "BYTES":
                # Connector serves BYTES as varbinary; CAST(varbinary AS varchar)
                # is illegal in Trino — to_base64 is the working encoding
                # (live-verified 2026-07-30).
                select_items.append(f"to_base64({quoted}) /* BYTES -> base64 string */")
            elif col_type == "TIMESTAMP":
                # Connector serves BQ TIMESTAMP as an ISO-8601 varchar with a 'T'
                # separator, which CAST(... AS timestamp) rejects —
                # from_iso8601_timestamp is the working parse (live-verified).
                select_items.append(
                    f"CAST(from_iso8601_timestamp({quoted}) AS timestamp(6)) /* connector serves TIMESTAMP as ISO-8601 varchar */"
                )
            elif col_type == "DATETIME":
                # Connector serves DATETIME as timestamp(3); Iceberg column is
                # timestamp(6) — widen explicitly.
                select_items.append(f"CAST({quoted} AS timestamp(6))")
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
        # DELETE first makes the load idempotent, matching the chunked path:
        # without it every re-run appends a full duplicate copy (live-verified
        # 2026-07-30 — three runs left target = 3x source). Iceberg DELETE is
        # a metadata operation; on a just-created empty table it is a no-op.
        return (
            f"-- Run the SOURCE DATABASE SETUP section first (creates the federated connector).\n"
            f"-- Execute in a workgroup with Athena engine v3.\n"
            f"-- Athena INSERT...SELECT (full table load; DELETE makes re-runs idempotent)\n"
            f"DELETE FROM {target};\n"
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
                (f"-- Run the SOURCE DATABASE SETUP section first (creates the federated connector). Execute in Athena engine v3.\n"
                f"-- STEP 0: Discover partition range — identify the real partition column and run:\n"
                f"-- SELECT MIN({{{{partition_field}}}}) AS min_val, MAX({{{{partition_field}}}}) AS max_val FROM {source};\n"
                f"-- Then generate chunk windows from min_val to max_val in {config.chunk_days}-day steps.\n"
                f"--\n"
                f"-- TEMPLATE: Athena INSERT...SELECT (chunked by {config.chunk_days}-day windows)\n"
                f"-- WARNING: Source uses ingestion-time partitioning (_PARTITIONTIME); "
                f"substitute the real ingestion-time column or _ingestion_time surrogate before running\n"
                f"-- Run this table's chunks SEQUENTIALLY (Iceberg optimistic locking — concurrent writes to one table can conflict).\n"
                f"-- Parallelize across DIFFERENT tables, up to the account's active-DML quota (default 100 in most regions, 200 in us-east-1; adjustable)\n"
                f"-- Each chunk is idempotent — the DELETE clears any partial prior attempt; safe to re-run\n"
                f"-- Each DML statement must finish within the Athena DML timeout (default 30 min, adjustable to 240) — split windows further if a chunk approaches it\n"
                f"DELETE FROM {target} WHERE {{{{partition_field}}}} >= DATE '{{{{start}}}}' AND {{{{partition_field}}}} < DATE '{{{{end}}}}';\n"
                f"INSERT INTO {target}\n"
                f"SELECT {select_clause} FROM {source}\n"
                f"WHERE {{{{partition_field}}}} >= DATE '{{{{start}}}}' AND {{{{partition_field}}}} < DATE '{{{{end}}}}';\n"
                f"-- Repeat for each {config.chunk_days}-day window across the partition range")
            ]

        raw_field = entity.time_partitioning.field if entity.time_partitioning else "partition_col"
        # Quote the partition field if it is a reserved word (Fix 2)
        field = quote_identifier(raw_field)
        # Source-side predicate: the federated connector serves BQ TIMESTAMP
        # columns as ISO-8601 varchar, so the INSERT's WHERE (which runs against
        # the connector) must parse it before comparing to DATE literals. The
        # DELETE runs against the Iceberg target where the column is a real
        # timestamp — it keeps the bare field. (Live-verified 2026-07-30.)
        src_field = field
        for col in entity.columns:
            if col.name == raw_field and col.field_type.upper() == "TIMESTAMP":
                src_field = f"from_iso8601_timestamp({field})"
                break
        chunk_days = config.chunk_days
        select_clause = self._build_select_clause(entity)

        # Emit concrete per-window statements so the deliverable is executable
        chunks = self._generate_chunk_windows(entity, chunk_days)

        if not chunks:
            # No date range available → emit discovery query + template
            return [
                (f"-- Run the SOURCE DATABASE SETUP section first (creates the federated connector). Execute in Athena engine v3.\n"
                f"-- STEP 0: Discover actual data range (no metadata date available)\n"
                f"SELECT MIN({field}) AS min_val, MAX({field}) AS max_val FROM {source};\n"
                f"-- Use the result to generate chunk windows from min_val to max_val in {chunk_days}-day steps.\n"),
                (f"-- TEMPLATE: Athena INSERT...SELECT (chunked by {chunk_days}-day windows on {raw_field})\n"
                f"-- Run this table's chunks SEQUENTIALLY (Iceberg optimistic locking — concurrent writes to one table can conflict).\n"
                f"-- Parallelize across DIFFERENT tables, up to the account's active-DML quota (default 100 in most regions, 200 in us-east-1; adjustable)\n"
                f"-- Each chunk is idempotent — the DELETE clears any partial prior attempt; safe to re-run\n"
                f"-- Each DML statement must finish within the Athena DML timeout (default 30 min, adjustable to 240) — split windows further if a chunk approaches it\n"
                f"DELETE FROM {target} WHERE {field} >= DATE '{{{{start}}}}' AND {field} < DATE '{{{{end}}}}';\n"
                f"INSERT INTO {target}\n"
                f"SELECT {select_clause} FROM {source}\n"
                f"WHERE {src_field} >= DATE '{{{{start}}}}' AND {src_field} < DATE '{{{{end}}}}';\n"
                f"-- Repeat for each {chunk_days}-day window across the partition range\n"
                f"-- FINAL CHUNK — rows with NULL {raw_field} (BigQuery __NULL__ partition):\n"
                f"-- DELETE FROM {target} WHERE {field} IS NULL;\n"
                f"-- INSERT INTO {target} SELECT {select_clause} FROM {source} WHERE {src_field} IS NULL;")
            ]

        statements = [
            (f"-- Run the SOURCE DATABASE SETUP section first (creates the federated connector). Execute in Athena engine v3.\n"
            f"-- STEP 0: verify the actual data range before running chunks (window bounds below derive from table metadata dates)\n"
            f"SELECT MIN({src_field}) AS min_val, MAX({src_field}) AS max_val FROM {source};\n"),
            (f"-- Athena INSERT...SELECT (chunked by {chunk_days}-day windows on {raw_field})\n"
            f"-- Windows derived from table metadata dates; extend/trim after STEP 0\n"
            f"-- Run this table's chunks SEQUENTIALLY (Iceberg optimistic locking — concurrent writes to one table can conflict).\n"
            f"-- Parallelize across DIFFERENT tables, up to the account's active-DML quota (default 100 in most regions, 200 in us-east-1; adjustable)\n"
            f"-- Each chunk is idempotent — the DELETE clears any partial prior attempt; safe to re-run\n"
            f"-- Each DML statement must finish within the Athena DML timeout (default 30 min, adjustable to 240) — split windows further if a chunk approaches it\n")
        ]

        # Emit up to first 5 chunk pairs fully, then summarize remainder
        for i, (start, end) in enumerate(chunks[:5]):
            statements.append(
                f"DELETE FROM {target} WHERE {field} >= DATE '{start}' AND {field} < DATE '{end}';\n"
                f"INSERT INTO {target}\n"
                f"SELECT {select_clause} FROM {source}\n"
                f"WHERE {src_field} >= DATE '{start}' AND {src_field} < DATE '{end}';\n"
            )

        if len(chunks) > 5:
            statements.append(
                f"\n-- ... plus {len(chunks) - 5} more chunks. Remaining windows:\n"
            )
            for start, end in chunks[5:]:
                statements.append(f"-- {start} to {end}\n")

        # NULL-partition chunk: BigQuery time-partitioned tables can hold rows
        # with a NULL partition column (the __NULL__ partition). Every date
        # window above excludes them — without this final chunk those rows
        # silently drop from the migration (2026-08-04 audit).
        statements.append(
            f"-- FINAL CHUNK: rows with NULL {raw_field} (BigQuery __NULL__ partition)\n"
            f"DELETE FROM {target} WHERE {field} IS NULL;\n"
            f"INSERT INTO {target}\n"
            f"SELECT {select_clause} FROM {source}\n"
            f"WHERE {src_field} IS NULL;\n"
        )

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
                remediation=(
                    f"One-time via EMR/Glue Spark: {spark_sort_command(table_iceberg, entity.clustering_fields)} "
                    "— S3 Tables managed compaction then keeps data sorted (auto strategy)"
                ),
                remediation_engine="spark",
            ))

        # Federated-connector readability gap: the BigQuery connector cannot
        # serve REPEATED (array) columns ("Lists have one child Field. Found:
        # none") and fails on STRUCTs containing timestamps — live-verified
        # 2026-07-30. The INSERT...SELECT path won't work for these tables;
        # they need a GCS-export (Parquet) load instead.
        connector_blocked = [
            col.name for col in entity.columns
            if col.mode == "REPEATED" or col.field_type.upper() in _CONNECTOR_UNREADABLE
        ]
        if connector_blocked:
            shortcomings.append(MigrationShortcoming(
                category="type_cast",
                severity="action_required",
                bq_source=f"columns: {connector_blocked}",
                description=(
                    "Athena BigQuery connector cannot read ARRAY/STRUCT/RANGE columns — "
                    "the federated INSERT...SELECT will fail for this table"
                ),
                remediation=(
                    "Load via GCS export instead: BigQuery EXPORT DATA (Parquet) → "
                    "S3 transfer → Athena INSERT...SELECT from an external Parquet "
                    "table (nested types survive Parquet round-trip)"
                ),
                remediation_engine="manual",
            ))

        # Type cast gap. Two tiers:
        # - action_required: types where the customer loses fidelity or must
        #   decide something (GEOGRAPHY→WKT, JSON→varchar, BIGNUMERIC precision,
        #   TIME→varchar, BYTES→base64, INTERVAL→varchar)
        # - advisory: TIMESTAMP/DATETIME — the generated SQL handles these
        #   end-to-end (connector serves them as varchar/timestamp(3)); nothing
        #   to decide, so flagging them "action required" on nearly every real
        #   table just trains customers to ignore the flag.
        _AUTOMATED_CASTS = ("TIMESTAMP", "DATETIME")
        decision_cols = [
            col.name for col in entity.columns
            if col.field_type.upper() in _TYPES_NEEDING_CAST
            and col.field_type.upper() not in _AUTOMATED_CASTS
        ]
        automated_cols = [
            col.name for col in entity.columns
            if col.field_type.upper() in _AUTOMATED_CASTS
        ]
        if decision_cols:
            bignumeric_cols = [
                col.name for col in entity.columns
                if col.field_type.upper() == "BIGNUMERIC"
            ]
            byte_cols = [
                col.name for col in entity.columns
                if col.field_type.upper() == "BYTES"
            ]
            base_desc = f"Columns {decision_cols} use types requiring CAST (emitted in generated SQL)"
            if bignumeric_cols:
                base_desc += ". BIGNUMERIC exceeds Athena DECIMAL(38) — out-of-range values become NULL via try_cast"

            base_remediation = "Review emitted CAST expressions"
            if byte_cols:
                base_remediation += (
                    f". BYTES columns {byte_cols} are stored base64-encoded "
                    "(to_base64 is emitted in the generated SQL); decode downstream "
                    "if binary fidelity is required"
                )
            if bignumeric_cols:
                base_remediation += (
                    f". BIGNUMERIC columns {bignumeric_cols}: values beyond DECIMAL(38,9) "
                    "become NULL — audit first with WHERE col IS NOT NULL AND "
                    "try_cast(col AS decimal(38,9)) IS NULL, or store as varchar "
                    "for full fidelity"
                )

            shortcomings.append(MigrationShortcoming(
                category="type_cast",
                severity="action_required",
                bq_source=f"columns: {decision_cols}",
                description=base_desc,
                remediation=base_remediation,
                remediation_engine="manual",
            ))
        if automated_cols:
            shortcomings.append(MigrationShortcoming(
                category="type_cast",
                severity="advisory",
                bq_source=f"columns: {automated_cols}",
                description=(
                    f"TIMESTAMP/DATETIME columns {automated_cols} are cast "
                    "automatically in the generated SQL (the connector serves "
                    "them as varchar/timestamp(3))"
                ),
                remediation="None needed — handled in the generated SQL",
                remediation_engine="athena",
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

        # Compaction advisory for large tables. S3 Tables runs automatic
        # compaction — and rejects Athena's OPTIMIZE/VACUUM — so the advice is
        # "nothing to do", not a command (the previous OPTIMIZE ... BIN_PACK
        # remediation contradicted the generated terraform and errors on
        # S3 Tables).
        threshold_bytes = config.compaction_threshold_gb * (1024 ** 3)
        if entity.num_bytes > threshold_bytes:
            shortcomings.append(MigrationShortcoming(
                category="compaction",
                severity="advisory",
                bq_source=f"table size: {entity.num_bytes / (1024**3):.2f} GB",
                description=(
                    f"Table exceeds {config.compaction_threshold_gb:.1f} GB; chunked loads "
                    "produce many small files"
                ),
                remediation=(
                    "None needed — S3 Tables compacts automatically (Athena's "
                    "OPTIMIZE/VACUUM are unsupported and unnecessary there). "
                    "Expect scan performance to improve within hours of load "
                    "as maintenance runs"
                ),
                remediation_engine="athena",
            ))

        return shortcomings
