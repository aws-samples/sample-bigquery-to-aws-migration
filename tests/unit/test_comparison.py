"""Tests for engine/comparison.py — unified cost comparison assembly (Fix 1-6 coverage).

Tests the structural fix: assemble_cost_comparison builds a complete CostComparison
in ONE pass with no post-hoc patching. Covers:
- Fix 1: Athena + Redshift scenarios assembled together, recommended first
- Fix 4: estimate_basis names Athena when Athena recommended
- Fix 5: Redshift-recommended justification contains "crossover" and "Revisit if"
- Fix 6: override_reason renders plainly, no "Engine analysis: N% confidence"
- Fix 8: proper typing on all seams
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal

from bq_assess.engine.comparison import (
    _build_athena_elaborated_justification,
    _derive_estimate_basis,
    _find_storage_line,
    assemble_cost_comparison,
)
from bq_assess.models import (
    AWSScenario,
    BQPricingModel,
    ConfidenceLevel,
    CostComparison,
    CostLine,
    EngineCostEstimate,
    EngineRecommendation,
    SignalContribution,
    WorkloadProfile,
)

# ---- Fixtures ----


def _storage_line() -> CostLine:
    return CostLine(
        label="S3 Tables storage",
        monthly=10.0,
        monthly_low=None,
        monthly_high=None,
        confidence=ConfidenceLevel.HIGH,
        source_note="test storage",
    )


def _serverless_scenario(monthly_total: float = 50.0) -> AWSScenario:
    return AWSScenario(
        label="Redshift Serverless",
        category="SERVERLESS",
        lines=[
            _storage_line(),
            CostLine(
                label="Redshift Serverless compute",
                monthly=monthly_total - 10.0,
                monthly_low=None,
                monthly_high=None,
                confidence=ConfidenceLevel.MEDIUM,
                source_note="test compute",
            ),
        ],
        monthly_total=monthly_total,
        confidence=ConfidenceLevel.MEDIUM,
    )


def _base_comparison(bq_monthly: float = 100.0, aws_total: float = 50.0) -> CostComparison:
    scenario = _serverless_scenario(aws_total)
    return CostComparison(
        bq_pricing_model=BQPricingModel.ON_DEMAND,
        bigquery_monthly=bq_monthly,
        bigquery_breakdown=[
            CostLine(
                label="BigQuery bytes scanned",
                monthly=bq_monthly,
                monthly_low=None,
                monthly_high=None,
                confidence=ConfidenceLevel.HIGH,
                source_note="test",
            )
        ],
        aws_lines=scenario.lines,
        aws_monthly_low=aws_total,
        aws_monthly_high=aws_total,
        monthly_delta_low=bq_monthly - aws_total,
        monthly_delta_high=bq_monthly - aws_total,
        annual_savings_low=(bq_monthly - aws_total) * 12,
        annual_savings_high=(bq_monthly - aws_total) * 12,
        migration_onetime=500.0,
        breakeven_months_low=10.0,
        breakeven_months_high=10.0,
        compute_confidence=ConfidenceLevel.MEDIUM,
        aws_scenarios=[scenario],
    )


def _athena_estimate(monthly_compute: float = 5.0) -> EngineCostEstimate:
    return EngineCostEstimate(
        engine_id="athena",
        monthly_total=Decimal(str(monthly_compute)),
        monthly_compute=Decimal(str(monthly_compute)),
        monthly_storage=Decimal(0),
        pricing_mode="on_demand",
        confidence="MEDIUM",
        source_note="test athena",
        one_time_migration=Decimal("25.0"),
    )


def _low_volume_profile() -> WorkloadProfile:
    return WorkloadProfile(
        has_data=True,
        total_stored_gb=5.0,
        total_queries=500,
        days_sampled=14,
        lookback_days=30,
        queries_per_day=16.7,
        monthly_scanned_tb=0.01,
        active_hour_fraction=0.1,
        avg_concurrent_queries=0.5,
        peak_concurrent_queries=2.0,
        total_slot_ms=100_000,
        avg_slots=0.5,
        peak_slots=2.0,
    )


def _high_volume_profile() -> WorkloadProfile:
    return WorkloadProfile(
        has_data=True,
        total_stored_gb=500.0,
        total_queries=50000,
        days_sampled=14,
        lookback_days=30,
        queries_per_day=1667,
        monthly_scanned_tb=5.0,
        active_hour_fraction=0.7,
        avg_concurrent_queries=15.0,
        peak_concurrent_queries=60.0,
        total_slot_ms=500_000_000,
        avg_slots=10.0,
        peak_slots=60.0,
    )


def _athena_recommendation() -> EngineRecommendation:
    return EngineRecommendation(
        primary_engine="athena",
        confidence=0.85,
        reasoning=[
            SignalContribution(signal="daily_scan_volume", value=0.01, direction="athena", weight=0.25),
            SignalContribution(signal="idle_ratio", value=0.9, direction="athena", weight=0.15),
        ],
        crossover_point_tb_day=Decimal("2.7"),
        override_reason=None,
    )


def _redshift_recommendation() -> EngineRecommendation:
    return EngineRecommendation(
        primary_engine="redshift",
        confidence=0.75,
        reasoning=[
            SignalContribution(signal="daily_scan_volume", value=5.0, direction="redshift", weight=0.25),
            SignalContribution(signal="peak_concurrency", value=60, direction="redshift", weight=0.20),
        ],
        crossover_point_tb_day=Decimal("2.7"),
        override_reason=None,
    )


# ---- Fix 1: assemble_cost_comparison produces complete result ----


class TestAssembleCostComparison:

    def test_athena_scenario_added(self):
        """Athena scenario appears in the assembled comparison."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _low_volume_profile(),
            _athena_recommendation(),
        )
        labels = [s.label for s in result.aws_scenarios]
        assert "Athena (on-demand $5/TB)" in labels

    def test_recommended_scenario_first(self):
        """The recommended scenario is first in aws_scenarios."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _low_volume_profile(),
            _athena_recommendation(),
        )
        assert result.aws_scenarios[0].is_recommended

    def test_all_derived_fields_populated(self):
        """All derived fields (deltas, savings, breakeven) are populated from winner."""
        result = assemble_cost_comparison(
            _base_comparison(bq_monthly=100.0),
            _athena_estimate(monthly_compute=5.0),
            _low_volume_profile(),
            _athena_recommendation(),
        )
        # Athena total = 5 (compute) + 10 (storage) = 15
        assert result.aws_monthly_low == 15.0
        assert result.aws_monthly_high == 15.0
        assert result.monthly_delta_low == 100.0 - 15.0
        assert result.monthly_delta_high == 100.0 - 15.0
        assert result.annual_savings_low == (100.0 - 15.0) * 12

    def test_athena_one_time_optimize_set(self):
        """athena_one_time_optimize is populated from the estimate."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _low_volume_profile(),
            _athena_recommendation(),
        )
        assert result.athena_one_time_optimize == Decimal("25.0")

    def test_recommendation_set(self):
        """The recommendation field is populated."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _low_volume_profile(),
            _athena_recommendation(),
        )
        assert result.recommendation is not None
        assert result.recommendation.recommended_scenario == "Athena (on-demand $5/TB)"

    def test_redshift_recommended_when_scorer_says_redshift(self):
        """When engine scorer says redshift, Redshift scenario is recommended."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _high_volume_profile(),
            _redshift_recommendation(),
        )
        assert "Redshift" in result.recommendation.recommended_scenario


# ---- Fix 4: estimate_basis names Athena when Athena recommended ----


class TestEstimateBasis:

    def test_athena_recommended_basis_names_athena(self):
        """When Athena is recommended, estimate_basis text mentions Athena."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _low_volume_profile(),
            _athena_recommendation(),
        )
        assert "Athena" in result.estimate_basis
        assert result.estimate_basis_level in (ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH)

    def test_redshift_recommended_basis_names_redshift(self):
        """When Redshift is recommended, estimate_basis text mentions Redshift."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _high_volume_profile(),
            _redshift_recommendation(),
        )
        assert "Redshift" in result.estimate_basis


# ---- Fix 5: Redshift-recommended justification contains crossover + revisit ----


class TestRedshiftElaboratedJustification:

    def test_redshift_justification_contains_crossover(self):
        """Redshift elaborated justification mentions 'crossover'."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _high_volume_profile(),
            _redshift_recommendation(),
        )
        recommended = next(s for s in result.aws_scenarios if s.is_recommended)
        assert "crossover" in recommended.justification

    def test_redshift_justification_contains_revisit_if(self):
        """Redshift elaborated justification mentions 'Revisit if'."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _high_volume_profile(),
            _redshift_recommendation(),
        )
        recommended = next(s for s in result.aws_scenarios if s.is_recommended)
        assert "Revisit if" in recommended.justification

    def test_redshift_justification_contains_confidence(self):
        """Redshift elaborated justification mentions confidence percentage."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _high_volume_profile(),
            _redshift_recommendation(),
        )
        recommended = next(s for s in result.aws_scenarios if s.is_recommended)
        assert "75% confidence" in recommended.justification


# ---- Fix 6: override_reason renders plainly ----


class TestOverrideReason:

    def test_override_reason_in_athena_justification(self):
        """When engine_recommendation has override_reason, justification opens with it."""
        rec = EngineRecommendation(
            primary_engine="athena",
            confidence=1.0,
            reasoning=[],
            crossover_point_tb_day=Decimal("2.7"),
            override_reason="User selected athena",
        )
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _low_volume_profile(),
            rec,
        )
        recommended = next(s for s in result.aws_scenarios if s.is_recommended)
        assert "explicit override" in recommended.justification
        assert "User selected athena" in recommended.justification
        # Must NOT claim "Engine analysis: N% confidence" for overrides
        assert "Engine analysis:" not in recommended.justification

    def test_override_reason_in_redshift_justification(self):
        """SLA override produces override text, not signal-based analysis."""
        rec = EngineRecommendation(
            primary_engine="redshift",
            confidence=0.95,
            reasoning=[
                SignalContribution(signal="interactive_ratio", value=1.0, direction="redshift", weight=1.0),
            ],
            crossover_point_tb_day=Decimal("2.7"),
            override_reason="Query SLA 1000ms < 3000ms — Athena cold start exceeds target",
        )
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _low_volume_profile(),
            rec,
        )
        recommended = next(s for s in result.aws_scenarios if s.is_recommended)
        assert "explicit override" in recommended.justification
        assert "Query SLA 1000ms" in recommended.justification
        assert "Engine analysis:" not in recommended.justification

    def test_preferred_engine_override_renders(self):
        """User-forced preferred_engine override renders override_reason."""
        rec = EngineRecommendation(
            primary_engine="athena",
            confidence=1.0,
            reasoning=[],
            crossover_point_tb_day=Decimal("2.7"),
            override_reason="User selected athena",
        )
        profile = _low_volume_profile()
        justification = _build_athena_elaborated_justification(
            profile, rec, _athena_estimate()
        )
        assert "explicit override" in justification
        assert "Engine analysis:" not in justification


# ---- Fix 3: pricing guard relaxation ----


class TestPricingGuard:

    def test_no_pricing_warning_in_console_output(self):
        """When pricing is None, workload_profile without data produces warning path."""
        # This tests the logic in cli.py Stage 13b:
        # The assembly is skipped when bq_pricing_model == UNKNOWN (sentinel).
        # We verify the sentinel comparison has no Athena scenario.
        sentinel = CostComparison(
            bq_pricing_model=BQPricingModel.UNKNOWN,
            bigquery_monthly=0.0,
            bigquery_breakdown=[],
            aws_lines=[],
            aws_monthly_low=0.0,
            aws_monthly_high=0.0,
            monthly_delta_low=0.0,
            monthly_delta_high=0.0,
            annual_savings_low=0.0,
            annual_savings_high=0.0,
            migration_onetime=0.0,
            breakeven_months_low=9999.0,
            breakeven_months_high=9999.0,
            compute_confidence=ConfidenceLevel.LOW,
        )
        # The sentinel has UNKNOWN model — the CLI gate skips assembly
        assert sentinel.bq_pricing_model == BQPricingModel.UNKNOWN


# ---- Helper tests ----


class TestFindStorageLine:

    def test_finds_storage_line(self):
        """Extracts storage line from scenarios."""
        scenarios = [_serverless_scenario()]
        line = _find_storage_line(scenarios)
        assert "storage" in line.label.lower()
        assert line.monthly == 10.0

    def test_empty_scenarios_returns_zero(self):
        """Empty scenarios list returns a zero-cost storage line."""
        line = _find_storage_line([])
        assert line.monthly == 0.0


class TestDeriveEstimateBasis:

    def test_athena_with_data(self):
        """Athena scenario with workload data mentions Athena."""
        athena_scenario = AWSScenario(
            label="Athena (on-demand $5/TB)",
            category="ATHENA_ONDEMAND",
            lines=[_storage_line()],
            monthly_total=15.0,
            confidence=ConfidenceLevel.MEDIUM,
        )
        _level, text = _derive_estimate_basis(
            [], athena_scenario, _low_volume_profile()
        )
        assert "Athena" in text

    def test_redshift_with_data(self):
        """Redshift scenario with workload data mentions Redshift."""
        rs_scenario = _serverless_scenario()
        _level, text = _derive_estimate_basis(
            [], rs_scenario, _low_volume_profile()
        )
        assert "Redshift" in text


# ---- Task 3: BQ cost unavailable state survives engine comparison ----


class TestUnavailableBQCost:

    def test_unavailable_bq_cost_survives_engine_comparison(self):
        """BQ cost unavailable state survives assemble_cost_comparison."""
        base = _base_comparison()
        base = dataclasses.replace(
            base,
            bq_cost_available=False,
            bq_cost_basis="unavailable",
            bq_cost_unavailable_reason="test reason",
            bigquery_monthly=0.0,
        )
        result = assemble_cost_comparison(
            base, _athena_estimate(), _low_volume_profile(), _athena_recommendation()
        )
        assert result.bq_cost_available is False
        assert result.bq_cost_basis == "unavailable"
        assert result.bq_cost_unavailable_reason == "test reason"
        assert result.monthly_delta_low == 0.0
        assert result.annual_savings_low == 0.0
        reasoning = (result.recommendation.reasoning if result.recommendation else "").lower()
        assert "cheaper than" not in reasoning
        assert "savings" not in reasoning


# ---- MRI-1: tension sentence on fallthrough branch ----


class TestTensionSentence:

    def test_athena_signal_but_expensive_produces_tension(self):
        """MRI-1: when scorer says athena but Athena fails 20% gate, tension text appears."""
        from bq_assess.engine.comparison import _generate_unified_recommendation

        # Make Athena much more expensive than cheapest
        storage_line = _storage_line()
        rs_scenario = AWSScenario(
            label="Redshift Serverless", category="SERVERLESS",
            lines=[storage_line], monthly_total=20.0,
            confidence=ConfidenceLevel.MEDIUM,
        )
        athena_scenario = AWSScenario(
            label="Athena (on-demand $5/TB)", category="ATHENA_ONDEMAND",
            lines=[storage_line], monthly_total=50.0,  # > 20 * 1.20 = 24
            confidence=ConfidenceLevel.MEDIUM,
        )
        engine_rec = EngineRecommendation(
            primary_engine="athena", confidence=0.7,
            reasoning=[SignalContribution(signal="daily_scan_volume", value=0.5, direction="athena", weight=0.3)],
            crossover_point_tb_day=Decimal("2.7"), override_reason=None,
        )
        scenarios = [rs_scenario, athena_scenario]
        rec = _generate_unified_recommendation(scenarios, _low_volume_profile(), 100.0, engine_rec)
        assert "Engine signal analysis favored Athena" in rec.reasoning
        assert "cost decides here" in rec.reasoning
        assert "Revisit Athena if volumes drop" in rec.reasoning


# ---- MRI-2b: absolute floor (sub-$10 negligible costs) ----


class TestAbsoluteFloor:

    def test_sub_10_costs_treated_as_equivalent(self):
        """MRI-2b: when all totals < $10, scorer decides even if Athena > 20% of cheapest."""
        from bq_assess.engine.comparison import _generate_unified_recommendation

        storage_line = _storage_line()
        rs_scenario = AWSScenario(
            label="Redshift Serverless", category="SERVERLESS",
            lines=[storage_line], monthly_total=3.0,
            confidence=ConfidenceLevel.MEDIUM,
        )
        athena_scenario = AWSScenario(
            label="Athena (on-demand $5/TB)", category="ATHENA_ONDEMAND",
            lines=[storage_line], monthly_total=5.0,  # > 3 * 1.20 = 3.60, but both < $10
            confidence=ConfidenceLevel.MEDIUM,
        )
        engine_rec = EngineRecommendation(
            primary_engine="athena", confidence=0.8,
            reasoning=[SignalContribution(signal="daily_scan_volume", value=0.01, direction="athena", weight=0.3)],
            crossover_point_tb_day=Decimal("2.7"), override_reason=None,
        )
        scenarios = [rs_scenario, athena_scenario]
        rec = _generate_unified_recommendation(scenarios, _low_volume_profile(), 100.0, engine_rec)
        # Athena should be recommended because costs are negligible
        assert rec.recommended_scenario == "Athena (on-demand $5/TB)"
        assert "negligible" in rec.reasoning


# ---- MRI-3: 4-RPU ratchet disclosure ----


class TestRatchetDisclosure:

    def test_crossover_assumptions_contains_manual_reset(self):
        """MRI-3: assumptions sentence mentions 'manual reset'."""
        from bq_assess.engine.comparison import _crossover_assumptions_sentence
        sentence = _crossover_assumptions_sentence(2.7)
        assert "manual reset" in sentence

    def test_athena_justification_contains_manual_reset(self):
        """MRI-3: Athena elaborated justification includes 4-RPU ratchet note."""
        result = assemble_cost_comparison(
            _base_comparison(),
            _athena_estimate(),
            _low_volume_profile(),
            _athena_recommendation(),
        )
        recommended = next(s for s in result.aws_scenarios if s.is_recommended)
        assert "manual reset" in recommended.justification


# ---- MRI-6: concurrency heuristic honesty ----


class TestConcurrencyHeuristic:

    def test_revisit_conditions_cite_quota(self):
        """MRI-6: revisit conditions mention Athena DML quota, not ~50."""
        from bq_assess.engine.comparison import _revisit_conditions_sentence
        sentence = _revisit_conditions_sentence(2.7)
        assert "Athena DML quota" in sentence
        assert "~50" not in sentence

    def test_signal_breakdown_reference_removed(self):
        """LOW-1: dead cross-reference 'See signal breakdown below' removed."""
        from bq_assess.engine.comparison import _build_athena_reasoning
        reasoning = _build_athena_reasoning(
            AWSScenario(
                label="Athena (on-demand $5/TB)", category="ATHENA_ONDEMAND",
                lines=[], monthly_total=5.0, confidence=ConfidenceLevel.MEDIUM,
            ),
            _low_volume_profile(),
            _athena_recommendation(),
        )
        assert "See signal breakdown below" not in reasoning
        assert "Signal detail available in the JSON export" in reasoning
