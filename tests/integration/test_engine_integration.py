"""Integration test: full engine recommendation + migration pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from bq_assess.core.engine_config import resolve_engine_config
from bq_assess.engine.athena.cost import AthenaCostEstimator
from bq_assess.engine.athena.migration import AthenaMigrationGenerator
from bq_assess.engine.athena.placement import AthenaPlacementAdvisor
from bq_assess.engine.athena.rewrite import AthenaRewriteGuide
from bq_assess.engine.recommendation import RecommendationScorer
from bq_assess.models import (
    BQPricingModel,
    ColumnSchema,
    ConfidenceLevel,
    ConversionResult,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    PricingDetection,
    TimePartitionConfig,
    WorkloadProfile,
)


def _profile_low_volume() -> WorkloadProfile:
    return WorkloadProfile(
        has_data=True,
        total_stored_gb=50.0,
        total_queries=1000,
        days_sampled=30,
        lookback_days=30,
        queries_per_day=33.0,
        queries_per_second_avg=0.0004,
        avg_concurrent_queries=0.5,
        peak_concurrent_queries=3.0,
        avg_bytes_per_query=500_000_000,
        monthly_scanned_tb=0.5,
        active_hour_fraction=0.1,
        total_slot_ms=5_000_000,
        avg_slots=0.002,
        p99_slots=0.01,
        peak_slots=0.05,
    )


def _profile_high_volume() -> WorkloadProfile:
    return WorkloadProfile(
        has_data=True,
        total_stored_gb=5000.0,
        total_queries=500_000,
        days_sampled=30,
        lookback_days=30,
        queries_per_day=16_666.0,
        queries_per_second_avg=0.19,
        avg_concurrent_queries=15.0,
        peak_concurrent_queries=80.0,
        avg_bytes_per_query=2_000_000_000,
        monthly_scanned_tb=30.0,
        active_hour_fraction=0.7,
        total_slot_ms=500_000_000_000,
        avg_slots=190.0,
        p99_slots=500.0,
        peak_slots=800.0,
    )


def test_full_pipeline_low_volume():
    """Low-volume workload → Athena recommended → migration DML generated."""
    profile = _profile_low_volume()
    config = resolve_engine_config(
        cli_params={"target_region": "ap-southeast-2", "post_optimization": True}
    )
    pricing = PricingDetection(
        model=BQPricingModel.ON_DEMAND,
        confidence=ConfidenceLevel.HIGH,
        source_note="test",
    )

    # Step 1: Recommendation
    scorer = RecommendationScorer()
    rec = scorer.recommend(profile, config)
    assert rec.primary_engine == "athena"

    # Step 2: Cost
    cost = AthenaCostEstimator().estimate_cost(profile, pricing)
    assert cost.monthly_compute > Decimal(0)
    assert cost.engine_id == "athena"

    # Step 3: Translation — assert real Trino output
    rewrite = AthenaRewriteGuide()
    bq_sql = "SELECT SAFE_DIVIDE(revenue, cost) AS margin FROM my_table"
    result = rewrite.translate(bq_sql)
    assert result.confidence in ("HIGH", "MEDIUM")
    # assert translation actually happened
    assert result.translated_sql, "Translation should produce non-empty SQL"
    assert "SELECT" in result.translated_sql, "Translated SQL must contain SELECT"
    # SAFE_DIVIDE is BQ-specific; sqlglot→trino should transform it
    assert "SAFE_DIVIDE" not in result.translated_sql, "BQ-specific SAFE_DIVIDE should be translated"

    # Step 4: Placement (for a view)
    advisor = AthenaPlacementAdvisor()
    view = EntityMetadata(
        entity_id="v1", dataset_id="ds", full_name="ds.my_view",
        entity_type=EntityType.VIEW, population=EntityPopulation.REBUILT,
        num_rows=0, num_bytes=0, columns=[], time_partitioning=None,
        range_partitioning=None, clustering_fields=None,
        view_query="SELECT * FROM ds.t", mview_query=None, routine=None,
        depends_on=["ds.t"], last_modified=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    placement = advisor.recommend(view)
    assert placement is not None
    assert placement.engine_id == "athena"

    # Step 5: Migration DML — assert real Athena INSERT syntax
    table = EntityMetadata(
        entity_id="t1", dataset_id="ds", full_name="ds.big_table",
        entity_type=EntityType.TABLE, population=EntityPopulation.TABLE,
        num_rows=10_000_000, num_bytes=5 * 1024**3,
        columns=[
            ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
            ColumnSchema(name="data", field_type="STRING", mode="NULLABLE"),
        ],
        time_partitioning=TimePartitionConfig(type="DAY", field="created_at"),
        range_partitioning=None, clustering_fields=["id"],
        view_query=None, mview_query=None, routine=None,
        depends_on=[], last_modified=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    conversion = ConversionResult(
        ddl="CREATE TABLE ...",
        partition_mapping=None,
        lossy_casts=[], warnings=[], success=True,
    )
    migration = AthenaMigrationGenerator()
    dml = migration.generate(table, conversion, config)

    # assert real INSERT syntax
    assert len(dml.statements) >= 1, "Should generate at least one migration statement"
    first_stmt = dml.statements[0]
    assert "INSERT INTO" in first_stmt, "Migration must use INSERT INTO syntax"
    assert "ds.big_table" in first_stmt, "INSERT must target correct table name"

    # assert shortcoming categories are exactly as expected
    shortcoming_categories = {s.category for s in dml.shortcomings}
    # This table has clustering_fields → sort_order shortcoming
    # No type-cast columns (INT64, STRING are direct-map)
    # Time-partitioned but field is explicit (not ingestion-time) → no partition_evolution
    # 5GB table exceeds 1.0GB threshold → compaction shortcoming
    assert shortcoming_categories == {"sort_order", "compaction"}, \
        f"Expected sort_order and compaction shortcomings; got {shortcoming_categories}"

    # explicit post_optimization check — S3 Tables compacts as managed
    # maintenance, so the only emitted step is the one-time sort declaration
    assert config.post_optimization is True, "Config should have post_optimization enabled"
    assert any(s.step_type == "sort" for s in dml.post_optimization), \
        "Post-optimization should carry the sort-order step for clustered tables"
    assert not any(s.step_type in ("compact", "vacuum") for s in dml.post_optimization), \
        "S3 Tables managed maintenance replaces self-managed compact/vacuum steps"


def test_full_pipeline_high_volume():
    """High-volume workload → Redshift recommended (guard against always-athena)."""
    profile = _profile_high_volume()
    config = resolve_engine_config(
        cli_params={"target_region": "ap-southeast-2", "post_optimization": True}
    )

    # Step 1: Recommendation — must pick Redshift for high-volume workload
    scorer = RecommendationScorer()
    rec = scorer.recommend(profile, config)
    assert rec.primary_engine == "redshift", \
        "High-volume workload (30TB/mo scanned, 190 avg slots) should recommend Redshift"
    assert rec.confidence > 0.5, "Recommendation confidence should be reasonably high"
