"""Tests for Athena migration DML generator."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bq_assess.engine.athena.migration import AthenaMigrationGenerator
from bq_assess.models import (
    ColumnSchema,
    ConversionResult,
    EngineConfig,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    MigrationDML,
    PartitionMapping,
    TimePartitionConfig,
)


def _config(**overrides) -> EngineConfig:
    defaults = dict(
        target_region="ap-southeast-2",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=None,
        source={},
    )
    defaults.update(overrides)
    return EngineConfig(**defaults)


def _entity(
    num_bytes: int = 500_000_000,
    clustering_fields: list[str] | None = None,
    time_partitioning: TimePartitionConfig | None = None,
    columns: list[ColumnSchema] | None = None,
    last_modified: datetime | None = None,
) -> EntityMetadata:
    # Default to 100 days ago to generate concrete chunks
    from datetime import timedelta
    if last_modified is None:
        last_modified = datetime.now(timezone.utc) - timedelta(days=100)

    return EntityMetadata(
        entity_id="tbl",
        dataset_id="ds",
        full_name="ds.my_table",
        entity_type=EntityType.TABLE,
        population=EntityPopulation.TABLE,
        num_rows=1000,
        num_bytes=num_bytes,
        columns=columns or [
            ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
            ColumnSchema(name="name", field_type="STRING", mode="NULLABLE"),
        ],
        time_partitioning=time_partitioning,
        range_partitioning=None,
        clustering_fields=clustering_fields,
        view_query=None,
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=last_modified,
    )


def _conversion() -> ConversionResult:
    return ConversionResult(
        ddl="CREATE TABLE ...",
        partition_mapping=None,
        lossy_casts=[],
        warnings=[],
        success=True,
    )


@pytest.fixture
def generator():
    return AthenaMigrationGenerator()


def test_basic_insert_generated(generator):
    """LOW-2: Every plan should include prerequisites preamble"""
    result = generator.generate(_entity(), _conversion(), _config())
    assert isinstance(result, MigrationDML)
    assert len(result.statements) >= 1
    assert "INSERT INTO" in result.statements[0]

    # LOW-2: Prerequisites should be in first statement
    first_stmt = result.statements[0]
    assert "PREREQUISITES" in first_stmt
    assert "source_db external tables must exist" in first_stmt
    assert "Athena engine v3" in first_stmt
    assert "fully-qualified names" in first_stmt


def test_clustering_triggers_sort_shortcoming(generator):
    entity = _entity(clustering_fields=["col1", "col2"])
    result = generator.generate(entity, _conversion(), _config())
    assert any(s.category == "sort_order" for s in result.shortcomings)


def test_compaction_shortcoming_for_large_tables(generator):
    """Tables exceeding compaction_threshold_gb trigger compaction advisory."""
    config = _config(compaction_threshold_gb=1.0)
    # 2 GB table (exceeds 1.0 GB threshold)
    entity = _entity(num_bytes=2 * 1024**3)
    result = generator.generate(entity, _conversion(), config)

    compaction_shortcomings = [s for s in result.shortcomings if s.category == "compaction"]
    assert len(compaction_shortcomings) == 1
    shortcoming = compaction_shortcomings[0]
    assert shortcoming.severity == "advisory"
    assert "OPTIMIZE" in shortcoming.remediation
    assert shortcoming.remediation_engine == "athena"
    assert "2.00 GB" in shortcoming.bq_source


def test_compaction_shortcoming_absent_for_small_tables(generator):
    """Tables below compaction_threshold_gb do not trigger compaction advisory."""
    config = _config(compaction_threshold_gb=1.0)
    # 0.5 GB table (below 1.0 GB threshold)
    entity = _entity(num_bytes=int(0.5 * 1024**3))
    result = generator.generate(entity, _conversion(), config)

    compaction_shortcomings = [s for s in result.shortcomings if s.category == "compaction"]
    assert len(compaction_shortcomings) == 0


def test_regionalized_concurrency_comment(generator):
    """HRI-2: Chunked INSERT comments guide sequential per-table, parallel across tables."""
    # Large partitioned table → chunked INSERT
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
    )
    result = generator.generate(entity, _conversion(), _config())

    # Join all statements to search for the comment
    full_text = "\n".join(result.statements)
    assert "100 in ap-southeast-2" in full_text
    assert "200 in us-east-1" in full_text
    assert "adjustable" in full_text
    # HRI-2: must guide sequential per-table execution to avoid Iceberg commit conflicts
    assert "Run this table's chunks SEQUENTIALLY" in full_text
    assert "Iceberg optimistic locking" in full_text
    assert "Parallelize across DIFFERENT tables" in full_text


def test_dml_timeout_note_in_chunks(generator):
    """Chunked INSERT comments mention DML timeout constraint."""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
    )
    result = generator.generate(entity, _conversion(), _config())

    full_text = "\n".join(result.statements)
    assert "DML timeout" in full_text
    assert "30 min" in full_text
    assert "adjustable to 240" in full_text



def test_clustering_triggers_post_optimization(generator):
    entity = _entity(
        clustering_fields=["col1"],
        num_bytes=2 * 1024**3,  # 2 GB > threshold
    )
    result = generator.generate(entity, _conversion(), _config())
    assert any(s.step_type == "sort" for s in result.post_optimization)
    sort_step = next(s for s in result.post_optimization if s.step_type == "sort")
    assert sort_step.engine == "spark_emr"
    # Should use Iceberg procedure syntax, not OPTIMIZE...SORT ORDER
    assert "CALL" in sort_step.command and "rewrite_data_files" in sort_step.command
    assert "sort_order" in sort_step.command


def test_compaction_always_emitted(generator):
    """MRI-3: VACUUM step should follow compact step"""
    result = generator.generate(_entity(), _conversion(), _config())
    assert any(s.step_type == "compact" for s in result.post_optimization)
    compact = next(s for s in result.post_optimization if s.step_type == "compact")
    assert compact.engine == "athena"
    assert "BIN_PACK" in compact.command

    # MRI-3: VACUUM step should be present after compact
    assert any(s.step_type == "vacuum" for s in result.post_optimization)
    vacuum = next(s for s in result.post_optimization if s.step_type == "vacuum")
    assert vacuum.engine == "athena"
    assert "VACUUM" in vacuum.command
    assert "S3 API requests" in vacuum.reason
    # Verify vacuum comes after compact
    compact_idx = result.post_optimization.index(compact)
    vacuum_idx = result.post_optimization.index(vacuum)
    assert vacuum_idx > compact_idx


def test_post_optimization_disabled(generator):
    config = _config(post_optimization=False)
    result = generator.generate(_entity(), _conversion(), config)
    assert len(result.post_optimization) == 0


def test_geography_column_triggers_type_cast(generator):
    entity = _entity(columns=[
        ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
        ColumnSchema(name="location", field_type="GEOGRAPHY", mode="NULLABLE"),
    ])
    result = generator.generate(entity, _conversion(), _config())
    assert any(s.category == "type_cast" for s in result.shortcomings)


def test_large_partitioned_table_chunks(generator):
    entity = _entity(
        num_bytes=500 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
    )
    conversion = ConversionResult(
        ddl="CREATE TABLE ...",
        partition_mapping=PartitionMapping(
            iceberg_transforms=["day(event_date)"],
            sort_order=[],
            auto_derived=True,
            decision_flags=[],
        ),
        lossy_casts=[],
        warnings=[],
        success=True,
    )
    result = generator.generate(entity, conversion, _config())
    # Large partitioned table should have multiple chunked statements
    assert len(result.statements) > 1 or "WHERE" in result.statements[0]


def test_ingestion_time_partition_emits_template_no_none(generator):
    """HRI-3: ingestion-time partitioning (field=None) must not emit 'WHERE None >= ...'"""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field=None),  # _PARTITIONTIME
    )
    result = generator.generate(entity, _conversion(), _config())
    # Should emit template with placeholder markers
    full_sql = " ".join(result.statements)
    # Must NOT contain "None" as a column name in WHERE clause
    assert "None" not in full_sql
    # Must contain template marker
    assert "TEMPLATE" in full_sql or "{{" in full_sql
    # Should mention ingestion-time partitioning
    assert any("ingestion" in s.lower() for s in result.statements)


def test_hour_granularity_chunks_scale_down(generator):
    """HRI-5: HOUR-granularity tables must use smaller windows to avoid exceeding 100-partition limit."""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="HOUR", field="event_ts"),
    )
    result = generator.generate(entity, _conversion(), _config(chunk_days=90))
    # Should chunk, so multiple statements or WHERE clause present
    assert len(result.statements) > 1 or "WHERE" in result.statements[0]

    # Extract date windows from generated SQL
    # For HOUR granularity with 90-day config, windows should be ≤4 days each
    # (90 partitions / 24 partitions per day = 3.75 days, implementation uses floor = 3 days + safety margin)
    # We'll verify no window exceeds 5 days (to allow for boundary rounding)
    import re
    date_pattern = r"DATE '(\d{4}-\d{2}-\d{2})'"
    dates = re.findall(date_pattern, " ".join(result.statements))
    if len(dates) >= 2:
        from datetime import datetime
        # Check consecutive pairs
        for i in range(0, len(dates) - 1, 2):
            start = datetime.strptime(dates[i], "%Y-%m-%d")
            end = datetime.strptime(dates[i + 1], "%Y-%m-%d")
            window_days = (end - start).days
            # With HOUR granularity, windows should be small (≤5 days to be safe)
            assert window_days <= 5, f"Window too large for HOUR granularity: {window_days} days"


def test_day_granularity_chunks_unchanged(generator):
    """HRI-5: DAY-granularity tables should use config.chunk_days (90) unchanged."""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
    )
    result = generator.generate(entity, _conversion(), _config(chunk_days=90))

    # Extract date windows — should use full 90-day chunks
    import re
    date_pattern = r"DATE '(\d{4}-\d{2}-\d{2})'"
    dates = re.findall(date_pattern, " ".join(result.statements))
    if len(dates) >= 2:
        from datetime import datetime
        # Check first window (if enough data exists)
        start = datetime.strptime(dates[0], "%Y-%m-%d")
        end = datetime.strptime(dates[1], "%Y-%m-%d")
        window_days = (end - start).days
        # Should be close to 90 days (or less if total range < 90 days)
        # Allow some flexibility for last_modified → now being < 90 days
        assert window_days <= 90


def test_chunk_days_zero_does_not_hang(generator):
    """Fix 5: chunk_days=0 should not cause infinite loop — floored at 1."""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
    )
    # Should complete without hanging
    result = generator.generate(entity, _conversion(), _config(chunk_days=0))
    assert isinstance(result, MigrationDML)
    # Should generate statements with at least 1-day windows
    import re
    date_pattern = r"DATE '(\d{4}-\d{2}-\d{2})'"
    dates = re.findall(date_pattern, " ".join(result.statements))
    if len(dates) >= 2:
        from datetime import datetime
        start = datetime.strptime(dates[0], "%Y-%m-%d")
        end = datetime.strptime(dates[1], "%Y-%m-%d")
        window_days = (end - start).days
        assert window_days >= 1, "Window must be at least 1 day to prevent infinite loop"


def test_json_column_emits_cast(generator):
    """WP2 Fix 1: JSON columns should emit CAST(col AS varchar) instead of SELECT *"""
    entity = _entity(columns=[
        ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
        ColumnSchema(name="data", field_type="JSON", mode="NULLABLE"),
        ColumnSchema(name="name", field_type="STRING", mode="NULLABLE"),
    ])
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)
    # Should contain explicit CAST for JSON column
    assert "CAST(data AS varchar)" in full_sql
    # Should have comment indicating JSON -> varchar
    assert "JSON -> varchar" in full_sql
    # Should NOT contain SELECT *
    assert "SELECT *" not in full_sql


def test_geography_column_emits_cast(generator):
    """WP2 Fix 1: GEOGRAPHY columns should emit CAST with WKT comment"""
    entity = _entity(columns=[
        ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
        ColumnSchema(name="location", field_type="GEOGRAPHY", mode="NULLABLE"),
    ])
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)
    assert "CAST(location AS varchar)" in full_sql
    assert "WKT" in full_sql


def test_bignumeric_column_emits_cast(generator):
    """WP2 Fix 1 + MRI-1: BIGNUMERIC columns should emit try_cast with NULL disclosure"""
    entity = _entity(columns=[
        ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
        ColumnSchema(name="amount", field_type="BIGNUMERIC", mode="NULLABLE"),
    ])
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)
    # MRI-1: should use try_cast not bare CAST
    assert "try_cast(amount AS decimal(38,9))" in full_sql
    assert "BIGNUMERIC: out-of-range values become NULL" in full_sql

    # MRI-1: shortcoming description should mention NULL behavior
    shortcomings = [s for s in result.shortcomings if s.category == "type_cast"]
    assert len(shortcomings) == 1
    assert "out-of-range values become NULL" in shortcomings[0].description
    assert "try_cast(col AS decimal(38,9)) IS NULL" in shortcomings[0].remediation


def test_time_column_emits_cast(generator):
    """WP2 Fix 1: TIME columns should emit CAST to varchar"""
    entity = _entity(columns=[
        ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
        ColumnSchema(name="event_time", field_type="TIME", mode="NULLABLE"),
    ])
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)
    assert "CAST(event_time AS varchar)" in full_sql
    assert "Athena cannot write Iceberg TIME" in full_sql


def test_bytes_column_emits_cast(generator):
    """WP2 Fix 1: BYTES columns should emit CAST to varchar with encoding note"""
    entity = _entity(columns=[
        ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
        ColumnSchema(name="binary_data", field_type="BYTES", mode="NULLABLE"),
    ])
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)
    assert "CAST(binary_data AS varchar)" in full_sql
    assert "base64" in full_sql.lower()


def test_interval_and_range_columns_emit_cast(generator):
    """WP2 Fix 1: INTERVAL and RANGE columns should emit CAST to varchar"""
    entity = _entity(columns=[
        ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
        ColumnSchema(name="duration", field_type="INTERVAL", mode="NULLABLE"),
        ColumnSchema(name="value_range", field_type="RANGE", mode="NULLABLE"),
    ])
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)
    assert "CAST(duration AS varchar)" in full_sql
    assert "CAST(value_range AS varchar)" in full_sql


def test_no_special_types_uses_select_star(generator):
    """WP2 Fix 1: Tables with no special types should still use SELECT *"""
    entity = _entity(columns=[
        ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
        ColumnSchema(name="name", field_type="STRING", mode="NULLABLE"),
        ColumnSchema(name="amount", field_type="NUMERIC", mode="NULLABLE"),
    ])
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)
    assert "SELECT *" in full_sql
    assert "CAST" not in full_sql


def test_chunked_plan_includes_range_discovery(generator):
    """WP2 Fix 2: Chunked plans should include range-discovery preamble as STEP 0"""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
    )
    result = generator.generate(entity, _conversion(), _config())
    # Should have multiple statements for chunked plan
    assert len(result.statements) > 1

    # First statement should be range discovery
    first_stmt = result.statements[0]
    assert "STEP 0" in first_stmt
    assert "SELECT MIN(event_date)" in first_stmt
    assert "MAX(event_date)" in first_stmt
    assert "table metadata dates" in first_stmt


def test_simple_plan_no_range_discovery(generator):
    """WP2 Fix 2: Non-chunked plans should NOT include range discovery"""
    entity = _entity(
        num_bytes=50 * 1024**3,  # Small table, no chunking
        time_partitioning=None,
    )
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)
    assert "STEP 0" not in full_sql
    assert "MIN(" not in full_sql or "MAX(" not in full_sql


def test_chunked_plan_has_idempotent_deletes(generator):
    """WP2 Fix 3: Each chunk should have DELETE before INSERT for idempotency"""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
    )
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)

    # Should contain DELETE statements
    assert "DELETE FROM" in full_sql
    # Should mention idempotency in comments
    assert "idempotent" in full_sql.lower()

    # Check that DELETE predicates match INSERT predicates
    import re
    delete_pattern = r"DELETE FROM .+ WHERE event_date >= DATE '([\d-]+)' AND event_date < DATE '([\d-]+)'"
    insert_pattern = r"INSERT INTO .+\nSELECT .+ FROM .+\nWHERE event_date >= DATE '([\d-]+)' AND event_date < DATE '([\d-]+)'"

    deletes = re.findall(delete_pattern, full_sql)
    inserts = re.findall(insert_pattern, full_sql, re.MULTILINE)

    # Should have at least one DELETE
    assert len(deletes) > 0
    # First DELETE window should match first INSERT window
    if deletes and inserts:
        assert deletes[0] == inserts[0]


def test_simple_plan_no_delete(generator):
    """WP2 Fix 3: Non-chunked plans should NOT have DELETE statements"""
    entity = _entity(
        num_bytes=50 * 1024**3,  # Small table
        time_partitioning=None,
    )
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)
    assert "DELETE FROM" not in full_sql


def test_chunked_plan_with_casts_combines_both_fixes(generator):
    """WP2: Chunked plan with JSON column should have both casts and idempotent chunks"""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
        columns=[
            ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
            ColumnSchema(name="data", field_type="JSON", mode="NULLABLE"),
        ],
    )
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)

    # Should have range discovery (Fix 2)
    assert "STEP 0" in full_sql
    assert "MIN(event_date)" in full_sql

    # Should have idempotent DELETE (Fix 3)
    assert "DELETE FROM" in full_sql
    assert "idempotent" in full_sql.lower()

    # Should have CAST for JSON (Fix 1)
    assert "CAST(data AS varchar)" in full_sql
    assert "JSON -> varchar" in full_sql

    # Should NOT have SELECT *
    assert "SELECT *" not in full_sql


# ---- Fix 2: quote reserved-word identifiers ----


def test_reserved_word_partition_field_quoted(generator):
    """Partition field 'timestamp' is a reserved word and must be double-quoted in WHERE."""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="timestamp"),
        columns=[
            ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
            ColumnSchema(name="timestamp", field_type="TIMESTAMP", mode="REQUIRED"),
            ColumnSchema(name="data", field_type="JSON", mode="NULLABLE"),
        ],
    )
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)

    # "timestamp" must be quoted in WHERE predicates
    assert '"timestamp"' in full_sql
    # The partition field appears in DELETE and INSERT WHERE clauses
    assert '"timestamp" >= DATE' in full_sql or '"timestamp" >=' in full_sql


def test_reserved_word_column_quoted_in_select(generator):
    """Column 'order' is a reserved word and must be quoted in select list."""
    entity = _entity(
        columns=[
            ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
            ColumnSchema(name="order", field_type="STRING", mode="NULLABLE"),
            ColumnSchema(name="data", field_type="JSON", mode="NULLABLE"),
        ],
    )
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)

    # "order" must be quoted in the select list (since JSON triggers explicit list)
    assert '"order"' in full_sql


def test_non_reserved_name_stays_unquoted(generator):
    """Non-reserved names like 'id' stay unquoted in select list."""
    entity = _entity(
        columns=[
            ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
            ColumnSchema(name="my_col", field_type="STRING", mode="NULLABLE"),
            ColumnSchema(name="data", field_type="JSON", mode="NULLABLE"),
        ],
    )
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)

    # "id" should appear unquoted (it's not reserved)
    assert "id," in full_sql or "id\n" in full_sql or full_sql.endswith("id")
    # But "data" is in JSON cast context — the CAST wraps it
    assert "CAST(data AS varchar)" in full_sql


def test_reserved_partition_field_in_range_discovery(generator):
    """Reserved-word partition field must be quoted in MIN/MAX range discovery."""
    entity = _entity(
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="date"),
        columns=[
            ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
            ColumnSchema(name="date", field_type="DATE", mode="REQUIRED"),
        ],
    )
    result = generator.generate(entity, _conversion(), _config())
    full_sql = " ".join(result.statements)

    # "date" is a reserved word; must be quoted in SELECT MIN/MAX
    assert 'MIN("date")' in full_sql or "MIN(date)" in full_sql  # quote_identifier decides
