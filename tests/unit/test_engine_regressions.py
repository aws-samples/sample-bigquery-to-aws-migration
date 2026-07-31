"""Tests for code-review findings fixes (2026-07-20)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bq_assess.cli import _build_workload_profile
from bq_assess.core.engine_config import resolve_engine_config
from bq_assess.engine.athena.migration import AthenaMigrationGenerator
from bq_assess.engine.recommendation import RecommendationScorer
from bq_assess.models import (
    ColumnSchema,
    ConversionResult,
    EngineConfig,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    PartitionMapping,
    SlotUtilization,
    TimePartitionConfig,
    WorkloadProfile,
)

# ---- WorkloadProfile missing fields ----


def test_finding_1_active_hour_fraction_wired():
    """active_hour_fraction must be wired from SlotUtilization."""
    slots = SlotUtilization(
        avg_slots=50.0,
        p50_slots=40.0,
        p99_slots=80.0,
        peak_slots=100.0,
        active_hour_fraction=0.75,  # 18 hours/day active
        total_slot_ms=1_000_000,
        days_sampled=7,
        total_queries=1000,
        lookback_days=7,
    )
    entities = [_make_entity("dataset.table1", num_bytes=100 * 1024**3)]
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=None,
        source={},
    )

    profile = _build_workload_profile(slots, entities, config)

    assert profile.active_hour_fraction == 0.75
    assert profile.peak_concurrent_queries == 100.0
    assert profile.avg_concurrent_queries == 50.0


def test_finding_1_scorer_uses_active_hour_fraction():
    """scorer must read active_hour_fraction (idle_ratio signal)."""
    # High active_hour_fraction → low idle_ratio → should favor Redshift
    profile = WorkloadProfile(
        has_data=True,
        active_hour_fraction=0.85,  # 20.4 hours/day active → idle_ratio 0.15
        monthly_scanned_tb=1.0,
        avg_slots=30.0,
        peak_slots=50.0,
        queries_per_day=100,
    )
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=None,
        source={},
    )

    scorer = RecommendationScorer()
    rec = scorer.recommend(profile, config)

    # Check that idle_ratio signal is not "athena" for a busy workload
    idle_signal = next((s for s in rec.reasoning if s.signal == "idle_ratio"), None)
    assert idle_signal is not None
    assert idle_signal.direction != "athena"  # should be "redshift" or "neutral"


# ---- Migration DML literal placeholders ----


def test_finding_2_chunked_dml_has_concrete_dates():
    """chunked INSERT must emit concrete dates, not {{start}}/{{end}}."""
    entity = _make_entity(
        "dataset.partitioned_table",
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
        last_modified=datetime.now(timezone.utc) - timedelta(days=365),
    )
    conversion = ConversionResult(
        ddl="CREATE TABLE ...",
        partition_mapping=PartitionMapping(
            iceberg_transforms=["day(event_date)"],
            sort_order=["event_date"],
            auto_derived=True,
            decision_flags=[],
        ),
        lossy_casts=[],
        warnings=[],
        success=True,
    )
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=None,
        source={},
    )

    gen = AthenaMigrationGenerator()
    result = gen.generate(entity, conversion, config)

    # Should have multiple statements (>365 days → ~4 chunks)
    assert len(result.statements) >= 4
    # Each statement should contain concrete DATE '2025-...' literals
    full_text = "\n".join(result.statements)
    assert "DATE '" in full_text
    # Must NOT contain unsubstituted placeholders
    assert "{{start}}" not in full_text
    assert "{{end}}" not in full_text


def test_finding_2_template_marker_when_no_date():
    """when no date range is available, emit -- TEMPLATE: marker."""
    entity = _make_entity(
        "dataset.partitioned_no_date",
        num_bytes=200 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
        last_modified=None,  # No creation date
    )
    conversion = ConversionResult(
        ddl="CREATE TABLE ...",
        partition_mapping=None,
        lossy_casts=[],
        warnings=[],
        success=True,
    )
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=None,
        source={},
    )

    gen = AthenaMigrationGenerator()
    result = gen.generate(entity, conversion, config)

    full_text = "\n".join(result.statements)
    assert "-- TEMPLATE:" in full_text
    assert "STEP 0" in full_text


# ---- Partition count chunking ----


def test_finding_3_chunks_high_partition_count():
    """chunk when partition count > 100, even if size < 100GB."""
    # 10GB table with 1100 daily partitions (3 years)
    entity = _make_entity(
        "dataset.many_partitions",
        num_bytes=10 * 1024**3,
        time_partitioning=TimePartitionConfig(type="DAY", field="event_date"),
        last_modified=datetime.now(timezone.utc) - timedelta(days=3 * 365),
    )
    conversion = ConversionResult(
        ddl="CREATE TABLE ...",
        partition_mapping=None,
        lossy_casts=[],
        warnings=[],
        success=True,
    )
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=None,
        source={},
    )

    gen = AthenaMigrationGenerator()
    result = gen.generate(entity, conversion, config)

    # Should be chunked (multiple statements)
    assert len(result.statements) > 1


def test_finding_3_no_chunk_small_partition_count():
    """don't chunk when partition count < 100 and size < 100GB."""
    # 50GB table with ~24 monthly partitions (2 years)
    entity = _make_entity(
        "dataset.few_partitions",
        num_bytes=50 * 1024**3,
        time_partitioning=TimePartitionConfig(type="MONTH", field="month_col"),
        last_modified=datetime.now(timezone.utc) - timedelta(days=2 * 365),
    )
    conversion = ConversionResult(
        ddl="CREATE TABLE ...",
        partition_mapping=None,
        lossy_casts=[],
        warnings=[],
        success=True,
    )
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=None,
        source={},
    )

    gen = AthenaMigrationGenerator()
    result = gen.generate(entity, conversion, config)

    # Should NOT be chunked (single simple INSERT)
    assert len(result.statements) == 1
    assert "simple" in result.statements[0].lower() or "full table" in result.statements[0].lower()


# ---- explicit --engine both loses to YAML ----


def test_finding_4_explicit_cli_both_wins_over_yaml():
    """explicit --engine both via CLI must override YAML preferred_engine."""
    resolved = resolve_engine_config(
        cli_params={
            "preferred_engine": None,  # "both" maps to None
            "_engine_cli_provided": True,  # Explicit CLI flag
        },
        yaml_config={
            "preferred_engine": "athena",  # YAML wants athena
        },
        prompt_responses={},
        inferred={},
    )

    # CLI should win → preferred_engine is None ("both")
    assert resolved.preferred_engine is None
    assert resolved.source["preferred_engine"] == "cli"


def test_finding_4_default_engine_loses_to_yaml():
    """default (not explicitly provided) loses to YAML."""
    resolved = resolve_engine_config(
        cli_params={
            "preferred_engine": None,
            "_engine_cli_provided": False,  # Not explicitly provided
        },
        yaml_config={
            "preferred_engine": "athena",
        },
        prompt_responses={},
        inferred={},
    )

    # YAML should win
    assert resolved.preferred_engine == "athena"
    assert resolved.source["preferred_engine"] == "yaml"


# ---- overrides not read ----


def test_finding_5_peak_concurrency_override_applied():
    """peak_concurrency_override must replace profile value."""
    slots = SlotUtilization(
        avg_slots=50.0,
        p50_slots=40.0,
        p99_slots=80.0,
        peak_slots=100.0,
        active_hour_fraction=0.75,
        total_slot_ms=1_000_000,
        days_sampled=7,
        total_queries=1000,
        lookback_days=7,
    )
    entities = [_make_entity("dataset.table1", num_bytes=100 * 1024**3)]
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=5,  # Override to 5
        idle_hours_override=None,
        source={},
    )

    profile = _build_workload_profile(slots, entities, config)

    # Override should replace the slots-derived value (100.0)
    assert profile.peak_concurrent_queries == 5.0


def test_finding_5_idle_hours_override_applied():
    """idle_hours_override must set active_hour_fraction."""
    slots = SlotUtilization(
        avg_slots=50.0,
        p50_slots=40.0,
        p99_slots=80.0,
        peak_slots=100.0,
        active_hour_fraction=0.75,
        total_slot_ms=1_000_000,
        days_sampled=7,
        total_queries=1000,
        lookback_days=7,
    )
    entities = [_make_entity("dataset.table1", num_bytes=100 * 1024**3)]
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=18.0,  # 18 hours idle/day → 6 hours active → 0.25
        source={},
    )

    profile = _build_workload_profile(slots, entities, config)

    # idle_hours=18 → active_hour_fraction = 1 - (18/24) = 0.25
    assert profile.active_hour_fraction == 0.25


def test_finding_5_override_affects_scorer():
    """override must change scorer's peak_concurrency signal."""
    # Without override: peak=80 → "neutral" or "redshift"
    # With override=5 → peak=5 → "athena"
    profile = WorkloadProfile(
        has_data=True,
        active_hour_fraction=0.5,
        monthly_scanned_tb=1.0,
        avg_slots=30.0,
        peak_slots=80.0,
        peak_concurrent_queries=5.0,  # Overridden to 5
        queries_per_day=100,
    )
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=None,
        source={},
    )

    scorer = RecommendationScorer()
    rec = scorer.recommend(profile, config)

    # Check peak_concurrency signal direction
    peak_signal = next((s for s in rec.reasoning if s.signal == "peak_concurrency"), None)
    assert peak_signal is not None
    assert peak_signal.direction == "athena"  # <20 → athena


# ---- burstiness distortion ----


def test_finding_6_burstiness_neutral_when_avg_slots_near_zero():
    """burstiness signal must be neutral when avg_slots ≈ 0."""
    profile = WorkloadProfile(
        has_data=True,
        active_hour_fraction=0.5,
        monthly_scanned_tb=0.01,
        avg_slots=0.0,  # Near-zero avg
        peak_slots=0.05,
        queries_per_day=10,
    )
    config = EngineConfig(
        target_region="us-east-1",
        query_sla_ms=5000,
        preferred_engine=None,
        chunk_days=90,
        post_optimization=True,
        compaction_threshold_gb=1.0,
        peak_concurrency_override=None,
        idle_hours_override=None,
        source={},
    )

    scorer = RecommendationScorer()
    rec = scorer.recommend(profile, config)

    # burstiness signal should be neutral (weight 0.0)
    burstiness_signal = next((s for s in rec.reasoning if s.signal == "burstiness_cv"), None)
    assert burstiness_signal is not None
    assert burstiness_signal.weight == 0.0
    assert burstiness_signal.direction == "neutral"


# ---- Helpers ----


def _make_entity(
    full_name: str,
    num_bytes: int = 1024**3,
    time_partitioning: TimePartitionConfig | None = None,
    last_modified: datetime | None = None,
) -> EntityMetadata:
    """Make a minimal EntityMetadata for testing."""
    parts = full_name.split(".")
    return EntityMetadata(
        entity_id=parts[1],
        dataset_id=parts[0],
        full_name=full_name,
        entity_type=EntityType.TABLE,
        population=EntityPopulation.TABLE,
        num_rows=1000,
        num_bytes=num_bytes,
        columns=[ColumnSchema(name="id", field_type="INTEGER", mode="REQUIRED", fields=[])],
        time_partitioning=time_partitioning,
        range_partitioning=None,
        clustering_fields=None,
        view_query=None,
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=last_modified or datetime.now(timezone.utc),
        physical_bytes=None,
    )
