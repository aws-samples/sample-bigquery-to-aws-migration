"""Tests for WP1 config fixes: region inference and YAML engine keys."""
from __future__ import annotations

from bq_assess.core.engine_config import resolve_engine_config
from bq_assess.engine.recommendation import RecommendationScorer
from bq_assess.models import WorkloadProfile


def test_region_inference_from_pricing_detection():
    """Fix 1: Inferred region from pricing detection flows into engine config."""
    # Simulate: pricing detected ap-southeast-2, no CLI/YAML/prompt region
    config = resolve_engine_config(
        cli_params={},
        yaml_config={},
        prompt_responses={},
        inferred={"target_region": "ap-southeast-2"},
    )

    assert config.target_region == "ap-southeast-2"
    assert config.source["target_region"] == "inferred"


def test_region_inference_with_crossover():
    """Fix 1: Inferred ap-southeast-2 region yields Sydney crossover (2.7 TB/day)."""
    # Build a workload profile with moderate scan volume
    # monthly_scanned_tb = 90 TB → 3.0 TB/day (above Sydney crossover)
    profile = WorkloadProfile(
        has_data=True,
        monthly_scanned_tb=90.0,
        days_sampled=30,
        peak_slots=500,
        active_hour_fraction=0.5,
        avg_concurrent_queries=50,
        peak_concurrent_queries=100,
    )

    # Config with inferred Sydney region
    config = resolve_engine_config(
        cli_params={},
        yaml_config={},
        prompt_responses={},
        inferred={"target_region": "ap-southeast-2"},
    )

    scorer = RecommendationScorer()
    rec = scorer.recommend(profile, config)

    # At 3.0 TB/day in Sydney (crossover ~2.7), Redshift should win
    assert rec.primary_engine == "redshift"
    # Crossover should be Sydney's 2.7, not us-east-1's 2.4
    assert 2.6 <= rec.crossover_point_tb_day <= 2.8


def test_yaml_engine_keys_parsed():
    """Fix 2: YAML migration and workload blocks are parsed."""
    yaml_config = {
        "target_region": "eu-west-1",
        "query_sla_ms": 3000,
        "preferred_engine": "athena",
        "chunk_days": 30,
        "post_optimization": False,
        "compaction_threshold_gb": 2.5,
        "peak_concurrency_override": 5,
        "idle_hours_override": 8.0,
    }

    config = resolve_engine_config(
        cli_params={},
        yaml_config=yaml_config,
        prompt_responses={},
        inferred={},
    )

    assert config.target_region == "eu-west-1"
    assert config.query_sla_ms == 3000
    assert config.preferred_engine == "athena"
    assert config.chunk_days == 30
    assert config.post_optimization is False
    assert config.compaction_threshold_gb == 2.5
    assert config.peak_concurrency_override == 5
    assert config.idle_hours_override == 8.0

    # All should be marked as from YAML
    assert config.source["target_region"] == "yaml"
    assert config.source["chunk_days"] == "yaml"
    assert config.source["peak_concurrency_override"] == "yaml"


def test_yaml_vs_cli_precedence():
    """Fix 2: CLI beats YAML for engine config keys."""
    yaml_config = {
        "target_region": "eu-west-1",
        "chunk_days": 30,
    }
    cli_params = {
        "target_region": "us-west-2",
        # chunk_days not in CLI
    }

    config = resolve_engine_config(
        cli_params=cli_params,
        yaml_config=yaml_config,
        prompt_responses={},
        inferred={},
    )

    # CLI wins for target_region
    assert config.target_region == "us-west-2"
    assert config.source["target_region"] == "cli"

    # YAML wins for chunk_days (CLI didn't provide it)
    assert config.chunk_days == 30
    assert config.source["chunk_days"] == "yaml"


def test_inferred_region_loses_to_yaml():
    """Fix 1+2: YAML region beats inferred region."""
    config = resolve_engine_config(
        cli_params={},
        yaml_config={"target_region": "eu-west-1"},
        prompt_responses={},
        inferred={"target_region": "ap-southeast-2"},
    )

    # YAML wins over inferred
    assert config.target_region == "eu-west-1"
    assert config.source["target_region"] == "yaml"


def test_absent_yaml_falls_back_to_defaults():
    """Fix 2: Missing YAML keys use defaults."""
    config = resolve_engine_config(
        cli_params={},
        yaml_config={},  # Empty YAML
        prompt_responses={},
        inferred={},
    )

    # Should fall back to defaults
    assert config.target_region == "us-east-1"
    assert config.chunk_days == 90
    assert config.post_optimization is True
    assert config.source["target_region"] == "default"
    assert config.source["chunk_days"] == "default"
