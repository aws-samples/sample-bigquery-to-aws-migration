"""Tests for engine config resolution (CLI > YAML > prompt > inferred)."""
from __future__ import annotations


from bq_assess.core.engine_config import resolve_engine_config


def test_cli_overrides_yaml():
    cli = {"target_region": "us-east-1", "query_sla_ms": 2000}
    yaml_cfg = {"target_region": "ap-southeast-2", "query_sla_ms": 5000}
    result = resolve_engine_config(cli_params=cli, yaml_config=yaml_cfg)
    assert result.target_region == "us-east-1"
    assert result.query_sla_ms == 2000


def test_yaml_fills_missing_cli():
    cli = {"target_region": "us-east-1"}
    yaml_cfg = {"query_sla_ms": 3000, "preferred_engine": "athena"}
    result = resolve_engine_config(cli_params=cli, yaml_config=yaml_cfg)
    assert result.target_region == "us-east-1"
    assert result.query_sla_ms == 3000
    assert result.preferred_engine == "athena"


def test_defaults_applied():
    result = resolve_engine_config(cli_params={}, yaml_config={})
    assert result.target_region == "us-east-1"
    assert result.query_sla_ms == 5000
    assert result.preferred_engine is None
    assert result.chunk_days == 90
    assert result.post_optimization is True
    assert result.compaction_threshold_gb == 1.0


def test_source_tracking():
    cli = {"target_region": "eu-west-1"}
    yaml_cfg = {"query_sla_ms": 3000}
    result = resolve_engine_config(cli_params=cli, yaml_config=yaml_cfg)
    assert result.source["target_region"] == "cli"
    assert result.source["query_sla_ms"] == "yaml"
    assert result.source["preferred_engine"] == "default"


def test_none_values_in_cli_dont_override():
    cli = {"target_region": None, "query_sla_ms": None}
    yaml_cfg = {"target_region": "ap-southeast-2"}
    result = resolve_engine_config(cli_params=cli, yaml_config=yaml_cfg)
    assert result.target_region == "ap-southeast-2"


def test_prompt_fills_missing_yaml():
    yaml_cfg = {"target_region": "us-west-2"}
    prompt_responses = {"query_sla_ms": 4000, "preferred_engine": "redshift"}
    result = resolve_engine_config(yaml_config=yaml_cfg, prompt_responses=prompt_responses)
    assert result.target_region == "us-west-2"
    assert result.query_sla_ms == 4000
    assert result.preferred_engine == "redshift"
    assert result.source["target_region"] == "yaml"
    assert result.source["query_sla_ms"] == "prompt"
    assert result.source["preferred_engine"] == "prompt"


def test_inferred_fills_missing_prompt():
    prompt_responses = {"target_region": "eu-central-1"}
    inferred = {"chunk_days": 60, "peak_concurrency_override": 10}
    result = resolve_engine_config(prompt_responses=prompt_responses, inferred=inferred)
    assert result.target_region == "eu-central-1"
    assert result.chunk_days == 60
    assert result.peak_concurrency_override == 10
    assert result.source["target_region"] == "prompt"
    assert result.source["chunk_days"] == "inferred"
    assert result.source["peak_concurrency_override"] == "inferred"


def test_full_precedence_chain():
    cli = {"target_region": "us-east-1", "query_sla_ms": 2000}
    yaml_cfg = {"target_region": "us-west-2", "query_sla_ms": 3000, "preferred_engine": "athena"}
    prompt_responses = {"preferred_engine": "redshift", "chunk_days": 60}
    inferred = {"chunk_days": 45, "compaction_threshold_gb": 2.5}

    result = resolve_engine_config(
        cli_params=cli,
        yaml_config=yaml_cfg,
        prompt_responses=prompt_responses,
        inferred=inferred
    )

    # CLI wins
    assert result.target_region == "us-east-1"
    assert result.query_sla_ms == 2000
    assert result.source["target_region"] == "cli"
    assert result.source["query_sla_ms"] == "cli"

    # YAML wins (CLI didn't have this)
    assert result.preferred_engine == "athena"
    assert result.source["preferred_engine"] == "yaml"

    # Prompt wins (CLI/YAML didn't have this)
    assert result.chunk_days == 60
    assert result.source["chunk_days"] == "prompt"

    # Inferred wins (CLI/YAML/prompt didn't have this)
    assert result.compaction_threshold_gb == 2.5
    assert result.source["compaction_threshold_gb"] == "inferred"

    # Default wins (no source had this)
    assert result.post_optimization is True
    assert result.source["post_optimization"] == "default"


def test_type_coercion():
    cli = {"query_sla_ms": "3500", "chunk_days": "120", "compaction_threshold_gb": "0.5"}
    result = resolve_engine_config(cli_params=cli)
    assert result.query_sla_ms == 3500
    assert isinstance(result.query_sla_ms, int)
    assert result.chunk_days == 120
    assert isinstance(result.chunk_days, int)
    assert result.compaction_threshold_gb == 0.5
    assert isinstance(result.compaction_threshold_gb, float)


def test_boolean_coercion():
    yaml_cfg = {"post_optimization": False}
    result = resolve_engine_config(yaml_config=yaml_cfg)
    assert result.post_optimization is False
    assert result.source["post_optimization"] == "yaml"


def test_false_overrides_default():
    cli = {"post_optimization": False}
    result = resolve_engine_config(cli_params=cli)
    assert result.post_optimization is False
    assert result.source["post_optimization"] == "cli"


def test_zero_overrides_default():
    yaml_cfg = {"query_sla_ms": 0}
    result = resolve_engine_config(yaml_config=yaml_cfg)
    assert result.query_sla_ms == 0
    assert result.source["query_sla_ms"] == "yaml"


def test_all_fields_have_source():
    result = resolve_engine_config(cli_params={})
    expected_fields = [
        "target_region", "query_sla_ms", "preferred_engine", "chunk_days",
        "post_optimization", "compaction_threshold_gb", "peak_concurrency_override",
        "idle_hours_override"
    ]
    for field in expected_fields:
        assert field in result.source, f"Missing source tracking for field: {field}"


def test_engine_both_maps_to_none():
    """Regression: --engine both should map to None so RecommendationScorer runs analysis."""
    # Simulate CLI mapping "both" → None (as cli.py does at line ~603)
    engine_param = "both"
    result = resolve_engine_config(
        cli_params={
            "preferred_engine": None if engine_param == "both" else engine_param,
        }
    )
    assert result.preferred_engine is None, \
        "CLI 'both' must map to None for scorer to run 8-signal analysis"


def test_engine_explicit_athena_stays():
    """Explicit --engine athena should pass through as 'athena'."""
    engine_param = "athena"
    result = resolve_engine_config(
        cli_params={
            "preferred_engine": None if engine_param == "both" else engine_param,
        }
    )
    assert result.preferred_engine == "athena"


def test_engine_explicit_redshift_stays():
    """Explicit --engine redshift should pass through as 'redshift'."""
    engine_param = "redshift"
    result = resolve_engine_config(
        cli_params={
            "preferred_engine": None if engine_param == "both" else engine_param,
        }
    )
    assert result.preferred_engine == "redshift"
