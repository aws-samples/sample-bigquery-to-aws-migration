"""RMS storage cost coverage (2026-08-03 design).

Two RMS byte pools feed the AWS cost breakdown on the Redshift path:
- TABLE entities Stage 13a placed on RMS (fidelity exception, ADR-0005)
- MATERIALIZED_VIEW entities Stage 13 homed in Redshift (a native Redshift MV
  stores its materialized result set in RMS)

The split must apply to EVERY Redshift scenario (not just the headline), must
never touch an Athena scenario, and must stash a pristine all-Iceberg storage
line so the Athena option (engine/comparison.py) prices the full estate as S3
Tables — nothing can live in RMS on an Athena deployment.
"""
from __future__ import annotations

from bq_assess.engine.redshift import cost_constants as k
from bq_assess.engine.redshift.cost import (
    _entity_physical_bytes,
    _line_value,
    apply_rms_storage_split,
    collect_rms_bytes,
)
from bq_assess.models import (
    AWSScenario,
    BQPricingModel,
    ConfidenceLevel,
    CostComparison,
    CostLine,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    PlacementRecommendation,
    StoragePlacement,
    StorageTarget,
)

_ONE_GB = 1024 ** 3

RMS_LINE_LABEL = "Redshift Managed Storage (RMS)"


def _entity(name: str, etype: EntityType, size_gb: float) -> EntityMetadata:
    population = (
        EntityPopulation.TABLE if etype in (EntityType.TABLE, EntityType.EXTERNAL)
        else EntityPopulation.REBUILT
    )
    return EntityMetadata(
        entity_id=name.split(".")[-1], dataset_id=name.split(".")[0], full_name=name,
        entity_type=etype, population=population,
        num_rows=1000, num_bytes=int(size_gb * _ONE_GB), columns=[],
        time_partitioning=None, range_partitioning=None, clustering_fields=None,
        view_query=None, mview_query=None, routine=None, depends_on=[],
        last_modified=None,
    )


def _storage_line(monthly: float) -> CostLine:
    return CostLine(
        label="S3 Tables storage", monthly=monthly,
        monthly_low=None, monthly_high=None,
        confidence=ConfidenceLevel.HIGH, source_note="test",
    )


def _compute_line(monthly: float) -> CostLine:
    return CostLine(
        label="Serverless compute", monthly=monthly,
        monthly_low=None, monthly_high=None,
        confidence=ConfidenceLevel.HIGH, source_note="test",
    )


def _scenario(label: str, category: str, storage: float, compute: float) -> AWSScenario:
    lines = [_storage_line(storage), _compute_line(compute)]
    return AWSScenario(
        label=label, category=category, lines=lines,
        monthly_total=round(storage + compute, 4),
        confidence=ConfidenceLevel.HIGH,
    )


def _comparison(scenarios: list[AWSScenario]) -> CostComparison:
    best = scenarios[0]
    total = best.monthly_total
    return CostComparison(
        bq_pricing_model=BQPricingModel.ON_DEMAND, bigquery_monthly=5000.0,
        bigquery_breakdown=[], aws_lines=best.lines,
        aws_monthly_low=total, aws_monthly_high=total,
        monthly_delta_low=5000.0 - total, monthly_delta_high=5000.0 - total,
        annual_savings_low=0.0, annual_savings_high=0.0,
        migration_onetime=100.0, breakeven_months_low=1.0, breakeven_months_high=1.0,
        compute_confidence=ConfidenceLevel.HIGH,
        aws_scenarios=scenarios,
    )


def _rms_placement() -> StoragePlacement:
    return StoragePlacement(
        target=StorageTarget.RMS, signals=["fidelity"], confidence=ConfidenceLevel.HIGH,
    )


def _iceberg_placement() -> StoragePlacement:
    return StoragePlacement(
        target=StorageTarget.ICEBERG, signals=[], confidence=ConfidenceLevel.HIGH,
    )


def _home(home: str) -> PlacementRecommendation:
    return PlacementRecommendation(
        home=home, signals=[], confidence=ConfidenceLevel.MEDIUM, refresh_unverified=False,
    )


# --------------------------------------------------------------- collect_rms_bytes


class TestCollectRmsBytes:
    def test_table_pool_from_rms_storage_placement(self):
        t1 = _entity("ds.rms_table", EntityType.TABLE, 10)
        t2 = _entity("ds.ice_table", EntityType.TABLE, 20)
        table_rms, mv_rms = collect_rms_bytes(
            [t1, t2],
            {"ds.rms_table": _rms_placement(), "ds.ice_table": _iceberg_placement()},
            {},
        )
        # Sized via the same physical-bytes fallback the S3 line uses
        assert table_rms == _entity_physical_bytes(t1)
        assert mv_rms == 0

    def test_mv_pool_from_redshift_home(self):
        mv = _entity("ds.mv1", EntityType.MATERIALIZED_VIEW, 5)
        table_rms, mv_rms = collect_rms_bytes([mv], {}, {"ds.mv1": _home("REDSHIFT")})
        assert table_rms == 0
        assert mv_rms == _entity_physical_bytes(mv)

    def test_iceberg_homed_mv_contributes_nothing(self):
        mv = _entity("ds.mv1", EntityType.MATERIALIZED_VIEW, 5)
        _, mv_rms = collect_rms_bytes([mv], {}, {"ds.mv1": _home("ICEBERG_CATALOG")})
        assert mv_rms == 0

    def test_views_and_routines_contribute_nothing(self):
        # Plain views and UDFs have no materialized storage even when Redshift-homed
        v = _entity("ds.v1", EntityType.VIEW, 3)
        r = _entity("ds.udf1", EntityType.ROUTINE, 1)
        table_rms, mv_rms = collect_rms_bytes(
            [v, r], {}, {"ds.v1": _home("REDSHIFT"), "ds.udf1": _home("REDSHIFT")},
        )
        assert table_rms == 0
        assert mv_rms == 0

    def test_empty_inputs(self):
        assert collect_rms_bytes([], {}, {}) == (0, 0)


# --------------------------------------------------------------- split behavior


class TestRmsSplitScenarios:
    def test_split_applies_to_every_redshift_scenario(self):
        """Every Redshift scenario's lines and monthly_total must carry the
        identical storage substitution — a headline-only split left scenario
        totals disagreeing with the breakdown."""
        s1 = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        s2 = _scenario("Serverless Reserved (1yr)", "SERVERLESS_1YR", 1000.0, 1600.0)
        s3 = _scenario("Provisioned (3yr RI)", "PROVISIONED_3YR", 1000.0, 1200.0)
        cc = _comparison([s1, s2, s3])
        cc.aws_lines = s1.lines  # headline shares the best scenario's list

        one_tb = 1024 ** 4
        apply_rms_storage_split(cc, one_tb, 10 * one_tb)

        for s in (s1, s2, s3):
            rms = [ln for ln in s.lines if ln.label == RMS_LINE_LABEL]
            assert len(rms) == 1, f"{s.label} missing RMS line"
            # scenario total reconciles with its own lines
            assert s.monthly_total == round(
                sum(_line_value(ln) for ln in s.lines), 4
            ), f"{s.label} total does not reconcile"

        # headline aws_lines got exactly ONE RMS line (no double-split of the
        # shared list) and headline totals derive from it
        assert sum(1 for ln in cc.aws_lines if ln.label == RMS_LINE_LABEL) == 1
        assert cc.aws_monthly_low == round(
            sum(_line_value(ln) for ln in cc.aws_lines), 4
        )

    def test_athena_scenario_never_touched(self):
        """Hardening: RMS cannot exist on an Athena deployment — the split must
        skip ATHENA-category scenarios entirely."""
        rs = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        athena = _scenario("Athena (on-demand $5/TB)", "ATHENA_ONDEMAND", 1000.0, 500.0)
        cc = _comparison([rs, athena])
        cc.aws_lines = rs.lines

        one_tb = 1024 ** 4
        apply_rms_storage_split(cc, one_tb, 10 * one_tb)

        assert not any("RMS" in ln.label for ln in athena.lines)
        assert athena.monthly_total == 1500.0  # untouched
        athena_storage = next(ln for ln in athena.lines if "storage" in ln.label.lower())
        assert athena_storage.monthly == 1000.0
        assert "moved to RMS" not in athena_storage.source_note

    def test_pristine_all_iceberg_line_stashed(self):
        """The split must stash a pre-split copy of the storage line for the
        Athena scenario assembled later — it prices ALL bytes as Iceberg."""
        rs = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        cc = _comparison([rs])
        cc.aws_lines = rs.lines

        one_tb = 1024 ** 4
        apply_rms_storage_split(cc, one_tb, 10 * one_tb)

        assert cc.all_iceberg_storage_line is not None
        assert cc.all_iceberg_storage_line.monthly == 1000.0
        assert "moved to RMS" not in cc.all_iceberg_storage_line.source_note
        # and the live line WAS reduced
        live = next(ln for ln in rs.lines if ln.label == "S3 Tables storage")
        assert live.monthly < 1000.0

    def test_no_rms_bytes_is_a_noop(self):
        rs = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        cc = _comparison([rs])
        apply_rms_storage_split(cc, 0, 10 * 1024 ** 4, mv_physical_bytes=0)
        assert cc.all_iceberg_storage_line is None
        assert not any("RMS" in ln.label for ln in rs.lines)


class TestRmsSplitMvPool:
    def test_mv_only_split_produces_rms_line(self):
        """An estate with zero fidelity tables but Redshift-homed MVs must still
        get the RMS line (the old `if rms_count` gate skipped it entirely)."""
        rs = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        cc = _comparison([rs])
        cc.aws_lines = rs.lines

        mv_bytes = 100 * _ONE_GB
        apply_rms_storage_split(cc, 0, 1000 * _ONE_GB, mv_physical_bytes=mv_bytes)

        rms = next(ln for ln in cc.aws_lines if ln.label == RMS_LINE_LABEL)
        expected = round(mv_bytes * k.GB_PER_BYTE * k.V6_MANAGED_STORAGE_USD_PER_GB_MONTH, 4)
        assert rms.monthly == expected
        assert "Redshift-native MVs" in rms.source_note
        assert "RMS-placed tables" not in rms.source_note

    def test_mv_bytes_drop_confidence_to_medium(self):
        """MV sizing is a proxy (BQ MV bytes, refresh churn unmodeled) — the RMS
        line is MEDIUM confidence when MV bytes contribute, HIGH when only
        DDL-generated table placements do."""
        rs1 = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        cc1 = _comparison([rs1])
        cc1.aws_lines = rs1.lines
        apply_rms_storage_split(cc1, 10 * _ONE_GB, 1000 * _ONE_GB, mv_physical_bytes=5 * _ONE_GB)
        rms1 = next(ln for ln in cc1.aws_lines if ln.label == RMS_LINE_LABEL)
        assert rms1.confidence == ConfidenceLevel.MEDIUM
        assert "RMS-placed tables" in rms1.source_note
        assert "Redshift-native MVs" in rms1.source_note

        rs2 = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        cc2 = _comparison([rs2])
        cc2.aws_lines = rs2.lines
        apply_rms_storage_split(cc2, 10 * _ONE_GB, 1000 * _ONE_GB)
        rms2 = next(ln for ln in cc2.aws_lines if ln.label == RMS_LINE_LABEL)
        assert rms2.confidence == ConfidenceLevel.HIGH

    def test_combined_pools_price_as_one_line(self):
        rs = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        cc = _comparison([rs])
        cc.aws_lines = rs.lines
        table_b, mv_b = 50 * _ONE_GB, 30 * _ONE_GB
        apply_rms_storage_split(cc, table_b, 1000 * _ONE_GB, mv_physical_bytes=mv_b)
        rms_lines = [ln for ln in cc.aws_lines if ln.label == RMS_LINE_LABEL]
        assert len(rms_lines) == 1
        expected = round(
            (table_b + mv_b) * k.GB_PER_BYTE * k.V6_MANAGED_STORAGE_USD_PER_GB_MONTH, 4
        )
        assert rms_lines[0].monthly == expected


class TestRmsRegionCorrectness:
    def test_rms_line_prices_at_applied_region_rate(self, monkeypatch):
        """The RMS line must follow apply_aws_region's V6 constant, not a
        hardcoded us-east-1 rate."""
        monkeypatch.setattr(k, "V6_MANAGED_STORAGE_USD_PER_GB_MONTH", 0.033)
        monkeypatch.setattr(k, "AWS_REGION_SCOPE", "Asia Pacific (Sydney) / ap-southeast-2")

        rs = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        cc = _comparison([rs])
        cc.aws_lines = rs.lines
        rms_bytes = 100 * _ONE_GB
        apply_rms_storage_split(cc, rms_bytes, 1000 * _ONE_GB)

        rms = next(ln for ln in cc.aws_lines if ln.label == RMS_LINE_LABEL)
        assert rms.monthly == round(rms_bytes * k.GB_PER_BYTE * 0.033, 4)
        assert "ap-southeast-2" in rms.source_note
        assert "$0.033/GB-mo" in rms.source_note


# --------------------------------------------------------------- Athena-path hardening


class TestAthenaPathNoRms:
    """Regression pins: when the recommended engine is Athena, NO RMS artifact
    can be produced anywhere. The CLI gates Stage 13a behind `not _is_athena`;
    these tests pin the invariants that make the gate sufficient."""

    def test_athena_placement_advisor_never_homes_in_redshift(self):
        """collect_rms_bytes counts MV bytes only for home == 'REDSHIFT'. The
        Athena advisor must never emit that home for ANY entity shape."""
        from bq_assess.engine.athena.placement import AthenaPlacementAdvisor
        from bq_assess.models import RoutineMetadata

        advisor = AthenaPlacementAdvisor()
        view = _entity("ds.v", EntityType.VIEW, 1)
        view.view_query = "SELECT 1"
        mv = _entity("ds.mv", EntityType.MATERIALIZED_VIEW, 1)
        mv.mview_query = "SELECT 1"
        js_udf = _entity("ds.f_js", EntityType.ROUTINE, 0)
        js_udf.routine = RoutineMetadata(
            name="f_js", language="JAVASCRIPT", arguments=[],
            body="return 1;", routine_type="SCALAR_FUNCTION",
        )
        sql_udf = _entity("ds.f_sql", EntityType.ROUTINE, 0)
        sql_udf.routine = RoutineMetadata(
            name="f_sql", language="SQL", arguments=[],
            body="(SELECT 1)", routine_type="SCALAR_FUNCTION",
        )
        proc = _entity("ds.p", EntityType.ROUTINE, 0)
        proc.routine = RoutineMetadata(
            name="p", language="SQL", arguments=[],
            body="BEGIN END", routine_type="PROCEDURE",
        )
        bare_routine = _entity("ds.f_bare", EntityType.ROUTINE, 0)

        for e in (view, mv, js_udf, sql_udf, proc, bare_routine):
            placement = advisor.recommend(e)
            if placement is not None:
                assert placement.home != "REDSHIFT", (
                    f"Athena advisor homed {e.full_name} in REDSHIFT — this would "
                    f"feed MV bytes into the RMS line on an Athena deployment"
                )

    def test_athena_homed_mvs_produce_zero_rms_bytes(self):
        """End-to-end pin: MVs carrying Athena-advisor homes contribute nothing,
        so the CLI's `if table_rms_bytes or mv_rms_bytes` never fires and the
        split never runs on the Athena path."""
        mv1 = _entity("ds.mv1", EntityType.MATERIALIZED_VIEW, 50)
        mv2 = _entity("ds.mv2", EntityType.MATERIALIZED_VIEW, 30)
        placements = {
            "ds.mv1": _home("UNSUPPORTED"),          # Athena MV verdict
            "ds.mv2": _home("LAMBDA_UDF_REQUIRED"),
        }
        # Athena path: storage_placement_results is always {} (Stage 13a gated off)
        table_rms, mv_rms = collect_rms_bytes([mv1, mv2], {}, placements)
        assert table_rms == 0
        assert mv_rms == 0


# --------------------------------------------------------------- comparison.py wiring


class TestAthenaScenarioUsesPristineLine:
    def test_build_athena_scenario_gets_full_estate_storage(self):
        """After a split, the Athena option must price the FULL estate as S3
        Tables via the stashed pristine line — not inherit the reduced line."""
        from decimal import Decimal

        from bq_assess.engine.comparison import assemble_cost_comparison
        from bq_assess.models import (
            EngineCostEstimate,
            EngineRecommendation,
            TargetEngine,
            WorkloadProfile,
        )

        rs = _scenario("Redshift Serverless", "SERVERLESS", 1000.0, 2000.0)
        cc = _comparison([rs])
        cc.aws_lines = rs.lines
        one_tb = 1024 ** 4
        apply_rms_storage_split(cc, one_tb, 10 * one_tb)
        reduced_s3 = next(
            ln for ln in rs.lines if ln.label == "S3 Tables storage"
        ).monthly
        assert reduced_s3 < 1000.0

        est = EngineCostEstimate(
            engine_id="athena", monthly_total=Decimal(500),
            monthly_compute=Decimal(500), monthly_storage=Decimal(0),
            pricing_mode="on_demand", confidence="MEDIUM", source_note="t",
            one_time_migration=Decimal(0),
        )
        profile = WorkloadProfile(has_data=True, monthly_scanned_tb=1.0, queries_per_day=10)
        rec = EngineRecommendation(
            primary_engine=TargetEngine.ATHENA, confidence=0.8,
            reasoning=[], crossover_point_tb_day=Decimal("4.0"),
            override_reason=None,
        )
        result = assemble_cost_comparison(cc, est, profile, rec)

        athena = next(s for s in result.aws_scenarios if "Athena" in s.label)
        athena_storage = next(ln for ln in athena.lines if "storage" in ln.label.lower())
        assert athena_storage.monthly == 1000.0, (
            "Athena option must price the full estate as S3 Tables (pristine line)"
        )
        assert not any("RMS" in ln.label for ln in athena.lines)
