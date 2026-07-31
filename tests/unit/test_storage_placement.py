"""Unit tests for engine/redshift/storage_placement.py (ADR-0005)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bq_assess.engine.redshift.storage_placement import StoragePlacementAdvisor
from bq_assess.models import (
    ColumnSchema,
    ConfidenceLevel,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    StorageTarget,
)
from bq_assess.scoring.effort import EffortScorer, amend_for_rms_placement

_ONE_GB = 1024**3


def _table(columns, num_bytes=10 * _ONE_GB, entity_type=EntityType.TABLE, name="ds.t1"):
    return EntityMetadata(
        entity_id=name,
        dataset_id=name.split(".")[0],
        full_name=name,
        entity_type=entity_type,
        population=EntityPopulation.TABLE if entity_type == EntityType.TABLE else EntityPopulation.REBUILT,
        num_rows=1000,
        num_bytes=num_bytes,
        columns=columns,
        time_partitioning=None,
        range_partitioning=None,
        clustering_fields=None,
        view_query=None,
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


def _col(name, ftype, mode="NULLABLE"):
    return ColumnSchema(name=name, field_type=ftype, mode=mode)


@pytest.fixture
def advisor():
    return StoragePlacementAdvisor(query_sla_ms=30_000)


def test_plain_table_defaults_to_iceberg(advisor):
    entity = _table([_col("id", "INT64"), _col("name", "STRING")])
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.ICEBERG
    assert sp.redshift_ddl is None
    assert sp.redshift_load is None


def test_geography_column_places_on_rms(advisor):
    entity = _table([_col("id", "INT64"), _col("boundary", "GEOGRAPHY")])
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.RMS
    assert any("GEOGRAPHY" in s for s in sp.signals)
    assert sp.confidence == ConfidenceLevel.HIGH


def test_json_and_interval_place_on_rms(advisor):
    entity = _table([_col("payload", "JSON"), _col("duration", "INTERVAL")])
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.RMS


def test_sub_second_sla_alone_stays_on_iceberg():
    """The SLA is workload-global — without a per-entity fidelity signal it must
    not flip storage (ADR-0005: Iceberg default, RMS exception)."""
    advisor = StoragePlacementAdvisor(query_sla_ms=500)
    entity = _table([_col("id", "INT64")])
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.ICEBERG
    assert any("Sub-second SLA" in s for s in sp.signals)
    assert any("Staying on Iceberg" in s for s in sp.signals)
    assert sp.confidence == ConfidenceLevel.MEDIUM


def test_size_ceiling_keeps_large_table_on_iceberg(advisor):
    entity = _table([_col("boundary", "GEOGRAPHY")], num_bytes=600 * _ONE_GB)
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.ICEBERG
    assert any("Size gate" in s for s in sp.signals)


def test_required_columns_alone_do_not_force_rms(advisor):
    """NOT NULL is informational — it must not flip storage by itself."""
    entity = _table([_col("id", "INT64", mode="REQUIRED")])
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.ICEBERG


def test_non_table_entities_stay_iceberg(advisor):
    entity = _table([_col("id", "INT64")], entity_type=EntityType.VIEW)
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.ICEBERG


def test_rms_ddl_maps_fidelity_types_natively(advisor):
    entity = _table(
        [
            _col("id", "INT64", mode="REQUIRED"),
            _col("boundary", "GEOGRAPHY"),
            _col("payload", "JSON"),
            _col("tags", "STRING", mode="REPEATED"),
        ],
        name="ds.geo_table",
    )
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.RMS
    ddl = sp.redshift_ddl
    assert "CREATE TABLE" in ddl
    assert "GEOMETRY" in ddl          # GEOGRAPHY → native spatial
    assert "SUPER" in ddl             # JSON + REPEATED → SUPER
    assert "BIGINT NOT NULL" in ddl   # REQUIRED enforced on RMS
    # Bounded DDL per ADR-0005 — no storage-coupled physical design
    assert "DISTKEY" not in ddl
    assert "SORTKEY" not in ddl


def test_rms_load_casts_fidelity_columns(advisor):
    entity = _table(
        [_col("id", "INT64"), _col("boundary", "GEOGRAPHY"), _col("payload", "JSON")],
        name="ds.geo_table",
    )
    sp = advisor.recommend(entity)
    load_sql = "\n".join(sp.redshift_load)
    assert "ST_GeomFromText" in load_sql   # WKT string → GEOMETRY
    assert "JSON_PARSE" in load_sql        # JSON string → SUPER
    assert "INSERT INTO" in load_sql
    assert "DROP TABLE" in load_sql        # staging cleanup (commented)


def test_rms_load_without_fidelity_uses_select_star(advisor):
    """The SELECT * fallback in _redshift_load (no per-column casts needed).

    Unreachable via recommend() since SLA-only placement stopped forcing RMS —
    tested directly to pin the staging-load contract."""
    entity = _table([_col("id", "INT64")])
    load = advisor._redshift_load(entity, fidelity_cols=[])
    assert "SELECT * FROM" in load[0]
    assert "INSERT INTO" in load[0]


def test_amend_for_rms_placement_adds_point_and_flag():
    scorer = EffortScorer()
    entity = _table([_col("id", "INT64")], num_bytes=1)
    from bq_assess.models import ConversionResult
    conversion = ConversionResult(
        ddl="CREATE ...", partition_mapping=None,
        lossy_casts=[], warnings=[], success=True,
    )
    base = scorer.score(entity, conversion)
    assert base.score == 0

    amended = amend_for_rms_placement(base)
    assert amended.score == base.score + 1
    assert "rms_two_phase_load" in amended.flags
    assert amended.category.value == "ASSISTED"
    assert "two-phase load" in amended.reasoning


def test_sla_reinforces_fidelity_placement():
    """SLA appears as a reinforcing signal when fidelity already justifies RMS."""
    advisor = StoragePlacementAdvisor(query_sla_ms=500)
    entity = _table([_col("boundary", "GEOGRAPHY")])
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.RMS
    assert any("Sub-second SLA" in s for s in sp.signals)
    assert sp.confidence == ConfidenceLevel.HIGH


def test_redshift_type_map_covers_converter_aliases():
    """_BQ_TO_REDSHIFT must stay a superset of the converter's BQ alias keys, or an
    alias lands as bigint in Iceberg staging but VARCHAR(65535) on RMS (finding F2)."""
    from bq_assess.engine.redshift.storage_placement import _BQ_TO_REDSHIFT
    from bq_assess.targets.iceberg.converter import CLEAN_TYPE_MAP, LOSSY_TYPE_MAP

    converter_keys = set(CLEAN_TYPE_MAP) | set(LOSSY_TYPE_MAP)
    missing = converter_keys - set(_BQ_TO_REDSHIFT)
    assert not missing, f"BQ types the converter maps but the RMS DDL map doesn't: {missing}"


def test_bigint_alias_maps_to_bigint_not_varchar(advisor):
    entity = _table(
        [_col("id", "BIGINT"), _col("small", "SMALLINT"), _col("boundary", "GEOGRAPHY")]
    )
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.RMS
    ddl_lines = sp.redshift_ddl.splitlines()
    assert any('"id" BIGINT' in ln or "id BIGINT" in ln for ln in ddl_lines)
    assert "VARCHAR(65535)" not in [
        ln for ln in ddl_lines if '"id"' in ln or '"small"' in ln
    ]


def test_dotless_full_name_does_not_crash(advisor):
    entity = _table([_col("boundary", "GEOGRAPHY")])
    entity.full_name = "bare_table_name"
    sp = advisor.recommend(entity)
    assert sp.target == StorageTarget.RMS
    assert "bare_table_name" in sp.redshift_ddl


def test_reprice_migration_effort_updates_onetime_and_breakeven():
    """Stage 13a amends effort after Stage 10 priced it — reprice must reconcile
    the cost summary with the amended per-entity scores (finding F1)."""
    from bq_assess.engine.redshift import cost_constants as k
    from bq_assess.engine.redshift.cost import reprice_migration_effort
    from bq_assess.models import BQPricingModel, CostComparison

    cost = CostComparison(
        bq_pricing_model=BQPricingModel.ON_DEMAND,
        bigquery_monthly=1000.0, bigquery_breakdown=[], aws_lines=[],
        aws_monthly_low=400.0, aws_monthly_high=500.0,
        monthly_delta_low=500.0, monthly_delta_high=600.0,
        annual_savings_low=6000.0, annual_savings_high=7200.0,
        migration_onetime=10 * k.MIGRATION_USD_PER_EFFORT_POINT,
        breakeven_months_low=0.1, breakeven_months_high=0.1,
        compute_confidence=ConfidenceLevel.HIGH,
    )
    reprice_migration_effort(cost, 13)  # 10 base points + 3 RMS amendments
    assert cost.migration_onetime == 13 * k.MIGRATION_USD_PER_EFFORT_POINT
    assert cost.breakeven_months_low == cost.migration_onetime / 500.0
    assert cost.breakeven_months_high == cost.migration_onetime / 600.0
    # run-rate figures untouched
    assert cost.monthly_delta_low == 500.0


def test_effort_category_ladder_single_source():
    """amend_for_rms_placement must use the same ladder as EffortScorer (finding F9)."""
    import inspect

    from bq_assess.scoring import effort as effort_mod

    src = inspect.getsource(effort_mod)
    # Exactly one ladder definition: the shared helper
    assert src.count("EffortCategory.ASSISTED") == 1


def test_apply_rms_storage_split_moves_bytes_to_rms_line():
    """Deep audit HRI-4: serverless bills RMS separately — RMS-placed bytes must
    leave the S3 Tables line and appear as an RMS line, with headline totals updated."""
    from bq_assess.engine.redshift import cost_constants as k
    from bq_assess.engine.redshift.cost import (
        _tiered_s3_tables_usd,
        apply_rms_storage_split,
    )
    from bq_assess.models import BQPricingModel, CostComparison, CostLine

    rms_bytes = 100 * 1024**3
    total_bytes = 1000 * 1024**3
    # The S3 line as estimate() would have priced it: tiered storage + monitoring.
    total_objects = total_bytes / (k.V2_ASSUMED_OBJECT_SIZE_MB * 1e6)
    s3_usd = round(
        _tiered_s3_tables_usd(total_bytes * k.GB_PER_BYTE)
        + total_objects / 1000.0 * k.V2_OBJECT_MONITORING_USD_PER_1K_OBJECTS_MONTH, 4)
    cost = CostComparison(
        bq_pricing_model=BQPricingModel.ON_DEMAND,
        bigquery_monthly=1000.0, bigquery_breakdown=[],
        aws_lines=[CostLine(label="S3 Tables storage", monthly=s3_usd,
                            monthly_low=None, monthly_high=None,
                            confidence=ConfidenceLevel.HIGH, source_note="test")],
        aws_monthly_low=100.0, aws_monthly_high=120.0,
        monthly_delta_low=880.0, monthly_delta_high=900.0,
        annual_savings_low=0.0, annual_savings_high=0.0,
        migration_onetime=50.0, breakeven_months_low=0.1, breakeven_months_high=0.1,
        compute_confidence=ConfidenceLevel.HIGH,
    )
    apply_rms_storage_split(cost, rms_bytes, total_bytes)

    rms_line = next(ln for ln in cost.aws_lines if "RMS" in ln.label)
    rms_gb = rms_bytes * k.GB_PER_BYTE  # same conversion the function uses
    expected_rms = round(rms_gb * k.V6_MANAGED_STORAGE_USD_PER_GB_MONTH, 4)
    assert rms_line.monthly == expected_rms
    # Reduced line = tier function + monitoring recomputed at the REMAINING volume
    # (marginal bytes leave the top occupied tier, not flat tier 1).
    remaining = total_bytes - rms_bytes
    remaining_objects = remaining / (k.V2_ASSUMED_OBJECT_SIZE_MB * 1e6)
    expected_reduced = round(
        _tiered_s3_tables_usd(remaining * k.GB_PER_BYTE)
        + remaining_objects / 1000.0 * k.V2_OBJECT_MONITORING_USD_PER_1K_OBJECTS_MONTH, 4)
    s3_line = next(ln for ln in cost.aws_lines if ln.label == "S3 Tables storage")
    assert s3_line.monthly == expected_reduced
    # RMS ($0.024) < S3 Tables tier1 ($0.0265): the total goes DOWN
    assert cost.aws_monthly_low < 100.0
    assert cost.monthly_delta_high == cost.bigquery_monthly - cost.aws_monthly_low


def test_apply_rms_storage_split_noop_without_rms_bytes():
    from bq_assess.engine.redshift.cost import apply_rms_storage_split
    from bq_assess.models import BQPricingModel, CostComparison

    cost = CostComparison(
        bq_pricing_model=BQPricingModel.ON_DEMAND,
        bigquery_monthly=0.0, bigquery_breakdown=[], aws_lines=[],
        aws_monthly_low=0.0, aws_monthly_high=0.0,
        monthly_delta_low=0.0, monthly_delta_high=0.0,
        annual_savings_low=0.0, annual_savings_high=0.0,
        migration_onetime=0.0, breakeven_months_low=0.0, breakeven_months_high=0.0,
        compute_confidence=ConfidenceLevel.LOW,
    )
    apply_rms_storage_split(cost, 0, 0)
    assert cost.aws_lines == []


def test_size_ceiling_labelled_as_opinion(advisor):
    """Deep audit: the 500GB ceiling has no AWS backing — the signal must say so."""
    entity = _table([_col("boundary", "GEOGRAPHY")], num_bytes=600 * _ONE_GB)
    sp = advisor.recommend(entity)
    assert any("practitioner judgment" in s for s in sp.signals)
