"""Tests for the engine recommendation scorer."""
from __future__ import annotations



from bq_assess.engine.recommendation import RecommendationScorer
from bq_assess.models import EngineConfig, WorkloadProfile


def _default_config(**overrides) -> EngineConfig:
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


def _low_volume_profile() -> WorkloadProfile:
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


def _high_volume_profile() -> WorkloadProfile:
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


def test_low_volume_recommends_athena():
    scorer = RecommendationScorer()
    result = scorer.recommend(_low_volume_profile(), _default_config())
    assert result.primary_engine == "athena"
    assert result.confidence > 0.5


def test_high_volume_recommends_redshift():
    scorer = RecommendationScorer()
    result = scorer.recommend(_high_volume_profile(), _default_config())
    assert result.primary_engine == "redshift"
    assert result.confidence > 0.5


def test_sla_override_forces_redshift():
    """SLA < 3000ms forces Redshift ONLY when cluster is warm (low idle ratio)."""
    scorer = RecommendationScorer()
    # Create a warm profile (active_hour_fraction > 0.7, so idle_ratio < 0.3)
    warm_profile = WorkloadProfile(
        has_data=True,
        total_stored_gb=100.0,
        total_queries=5000,
        days_sampled=30,
        lookback_days=30,
        queries_per_day=166.0,
        queries_per_second_avg=0.002,
        avg_concurrent_queries=2.0,
        peak_concurrent_queries=10.0,
        avg_bytes_per_query=100_000_000,
        monthly_scanned_tb=1.5,
        active_hour_fraction=0.75,  # idle_ratio = 0.25 < 0.3 threshold
        total_slot_ms=50_000_000,
        avg_slots=0.02,
        p99_slots=0.1,
        peak_slots=0.2,
    )
    config = _default_config(query_sla_ms=2000)
    result = scorer.recommend(warm_profile, config)
    assert result.primary_engine == "redshift"
    assert result.override_reason is not None
    assert "SLA" in result.override_reason or "cold" in result.override_reason.lower()


def test_user_preferred_engine_forces_choice():
    scorer = RecommendationScorer()
    config = _default_config(preferred_engine="athena")
    result = scorer.recommend(_high_volume_profile(), config)
    assert result.primary_engine == "athena"
    assert result.confidence == 1.0
    assert result.override_reason is not None


def test_no_workload_data_recommends_athena():
    scorer = RecommendationScorer()
    profile = WorkloadProfile(has_data=False, total_stored_gb=10.0)
    result = scorer.recommend(profile, _default_config())
    assert result.primary_engine == "athena"


def test_crossover_point_is_positive():
    scorer = RecommendationScorer()
    result = scorer.recommend(_low_volume_profile(), _default_config())
    assert result.crossover_point_tb_day > 0


def test_reasoning_contains_signal_contributions():
    scorer = RecommendationScorer()
    result = scorer.recommend(_low_volume_profile(), _default_config())
    assert len(result.reasoning) > 0
    for contribution in result.reasoning:
        assert contribution.signal in (
            "daily_scan_volume", "idle_ratio", "burstiness_cv",
            "peak_concurrency", "compute_intensity", "cache_hit_rate",
            "interactive_ratio", "distinct_users", "sla_warmth_caveat",
        )


def test_sla_override_with_busy_profile():
    """SLA < 3000ms with busy profile (active_hour_fraction=0.8) forces Redshift."""
    scorer = RecommendationScorer()
    busy_profile = WorkloadProfile(
        has_data=True,
        total_stored_gb=100.0,
        total_queries=5000,
        days_sampled=30,
        lookback_days=30,
        queries_per_day=166.0,
        queries_per_second_avg=0.002,
        avg_concurrent_queries=2.0,
        peak_concurrent_queries=10.0,
        avg_bytes_per_query=100_000_000,
        monthly_scanned_tb=1.5,
        active_hour_fraction=0.8,  # busy cluster
        total_slot_ms=50_000_000,
        avg_slots=0.02,
        p99_slots=0.1,
        peak_slots=0.2,
    )
    config = _default_config(query_sla_ms=2000)
    result = scorer.recommend(busy_profile, config)
    assert result.primary_engine == "redshift"
    assert result.override_reason is not None
    assert "SLA" in result.override_reason or "cold" in result.override_reason.lower()


def test_sla_override_with_idle_profile():
    """MRI-5: SLA < 3000ms with idle profile lets scoring run, caveat suppresses interactive_ratio."""
    scorer = RecommendationScorer()
    idle_profile = WorkloadProfile(
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
        active_hour_fraction=0.1,  # idle cluster (idle_ratio=0.9 >= 0.3)
        total_slot_ms=5_000_000,
        avg_slots=0.002,
        p99_slots=0.01,
        peak_slots=0.05,
    )
    config = _default_config(query_sla_ms=2000)
    result = scorer.recommend(idle_profile, config)
    # Should NOT override to Redshift, should run normal scoring
    assert result.override_reason is None
    # Should have the warmth caveat signal
    caveat_signals = [c for c in result.reasoning if c.signal == "sla_warmth_caveat"]
    assert len(caveat_signals) == 1
    assert caveat_signals[0].direction == "neutral"
    assert caveat_signals[0].weight == 0.0

    # MRI-5: interactive_ratio signal should be neutral (suppressed by warmth caveat)
    interactive_signals = [c for c in result.reasoning if c.signal == "interactive_ratio"]
    assert len(interactive_signals) == 1
    assert interactive_signals[0].direction == "neutral"
    assert interactive_signals[0].weight == 0.0
    assert len(caveat_signals) == 1
    assert caveat_signals[0].direction == "neutral"
    assert caveat_signals[0].weight == 0.0
    # idle_ratio = 1.0 - 0.1 = 0.9
    assert caveat_signals[0].value == 0.9


def test_sla_normal_range_unchanged():
    """SLA >= 3000ms should have normal behavior (no override)."""
    scorer = RecommendationScorer()
    config = _default_config(query_sla_ms=5000)
    result = scorer.recommend(_low_volume_profile(), config)
    # Low volume profile should recommend Athena
    assert result.primary_engine == "athena"
    assert result.override_reason is None
    # Should NOT have the warmth caveat signal
    caveat_signals = [c for c in result.reasoning if c.signal == "sla_warmth_caveat"]
    assert len(caveat_signals) == 0


# --- Unified recommendation integration: aws_lines/ordering track the winner ----

def test_athena_recommended_aws_lines_contain_athena_no_redshift():
    """When Athena is the unified recommendation, aws_lines must contain an Athena
    compute line and NO Redshift compute line. The recommended scenario must be first
    in aws_scenarios (defects 1, 2, 5 regression guard)."""
    from bq_assess.engine.comparison import _generate_unified_recommendation
    from bq_assess.models import (
        AWSScenario, CostLine, ConfidenceLevel, EngineRecommendation,
        SignalContribution,
    )

    # Build a simple Redshift scenario
    storage_line = CostLine(
        label="S3 Tables storage", monthly=0.12, monthly_low=None, monthly_high=None,
        confidence=ConfidenceLevel.MEDIUM, source_note="test",
    )
    compute_line = CostLine(
        label="Redshift Serverless compute", monthly=0.17, monthly_low=None,
        monthly_high=None, confidence=ConfidenceLevel.MEDIUM, source_note="test",
    )
    rs_scenario = AWSScenario(
        label="Redshift Serverless (On-Demand)", category="SERVERLESS",
        lines=[compute_line, storage_line], monthly_total=0.29,
        confidence=ConfidenceLevel.MEDIUM, is_recommended=True,
        justification="test", cluster_config="", workload_fit_notes=[],
        not_recommended_reason="",
    )

    # Build an Athena scenario (cheaper)
    athena_compute = CostLine(
        label="Athena compute (on-demand $5/TB)", monthly=0.06, monthly_low=None,
        monthly_high=None, confidence=ConfidenceLevel.MEDIUM, source_note="test",
    )
    athena_scenario = AWSScenario(
        label="Athena (on-demand $5/TB)", category="ATHENA_ONDEMAND",
        lines=[athena_compute, storage_line], monthly_total=0.18,
        confidence=ConfidenceLevel.MEDIUM, is_recommended=False,
        justification="test", cluster_config="", workload_fit_notes=[],
        not_recommended_reason="",
    )

    # Engine recommendation says "athena"
    engine_rec = EngineRecommendation(
        primary_engine="athena", confidence=0.8, override_reason=None,
        reasoning=[SignalContribution(signal="daily_scan_volume", value=0.5, direction="athena", weight=0.3)],
        crossover_point_tb_day=1.0,
    )

    wp = _low_volume_profile()
    scenarios = [rs_scenario, athena_scenario]
    bigquery_monthly = 0.29

    # Run unified recommendation
    rec = _generate_unified_recommendation(scenarios, wp, bigquery_monthly, engine_rec)
    assert rec.recommended_scenario == "Athena (on-demand $5/TB)"

    # Mark scenarios as the CLI does
    for s in scenarios:
        s.is_recommended = (s.label == rec.recommended_scenario)
        if s.is_recommended:
            s.not_recommended_reason = ""

    best = next(s for s in scenarios if s.is_recommended)

    # DEFECT 1 & 5: aws_lines must come from the Athena scenario
    aws_lines = best.lines
    labels = " ".join(line.label for line in aws_lines)
    assert "Athena" in labels, f"Expected Athena in aws_lines, got: {labels}"
    assert "Redshift Serverless compute" not in labels, (
        f"Redshift compute must NOT appear in aws_lines when Athena is recommended: {labels}"
    )

    # DEFECT 2: recommended scenario must be first after sorting
    scenarios.sort(key=lambda s: (not s.is_recommended, s.monthly_total))
    assert scenarios[0].is_recommended, "Recommended scenario must be first"
    assert scenarios[0].label == "Athena (on-demand $5/TB)"
