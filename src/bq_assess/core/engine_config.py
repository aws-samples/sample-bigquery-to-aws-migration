"""Engine config resolution — merges CLI, YAML, prompts, and inferred defaults.

Precedence: CLI flags > YAML config > Interactive prompt > Inferred/defaults.
"""
from __future__ import annotations

from bq_assess.models import EngineConfig

_DEFAULTS = {
    "target_region": "us-east-1",
    "query_sla_ms": 5000,
    "preferred_engine": None,
    "chunk_days": 90,
    "post_optimization": True,
    "compaction_threshold_gb": 1.0,
    "peak_concurrency_override": None,
    "idle_hours_override": None,
}


def resolve_engine_config(
    cli_params: dict | None = None,
    yaml_config: dict | None = None,
    prompt_responses: dict | None = None,
    inferred: dict | None = None,
) -> EngineConfig:
    """Merge config sources by precedence into a resolved EngineConfig.

    Args:
        cli_params: Command-line arguments (highest precedence)
        yaml_config: Values from config file
        prompt_responses: Interactive prompt responses
        inferred: Values inferred from dataset analysis (lowest precedence)

    Returns:
        Fully-resolved EngineConfig with source tracking
    """
    cli_params = cli_params or {}
    yaml_config = yaml_config or {}
    prompt_responses = prompt_responses or {}
    inferred = inferred or {}

    # An explicit CLI --engine (even "both" → None) must outrank YAML
    engine_cli_provided = cli_params.pop("_engine_cli_provided", False)
    region_is_fallback = inferred.pop("_region_is_fallback", False)

    source: dict[str, str] = {}
    resolved: dict = {}

    for key, default in _DEFAULTS.items():
        # Explicit CLI engine wins even when it resolves to None ("both")
        if key == "preferred_engine" and engine_cli_provided:
            resolved[key] = cli_params.get(key)
            source[key] = "cli"
        # Check each layer in precedence order
        elif cli_params.get(key) is not None:
            resolved[key] = cli_params[key]
            source[key] = "cli"
        elif yaml_config.get(key) is not None:
            resolved[key] = yaml_config[key]
            source[key] = "yaml"
        elif prompt_responses.get(key) is not None:
            resolved[key] = prompt_responses[key]
            source[key] = "prompt"
        elif inferred.get(key) is not None:
            resolved[key] = inferred[key]
            # Mark target_region as "fallback" when it's an unknown-location default
            if key == "target_region" and region_is_fallback:
                source[key] = "fallback"
            else:
                source[key] = "inferred"
        else:
            resolved[key] = default
            source[key] = "default"

    return EngineConfig(
        target_region=resolved["target_region"],
        query_sla_ms=int(resolved["query_sla_ms"]),
        preferred_engine=resolved["preferred_engine"],
        chunk_days=int(resolved["chunk_days"]),
        post_optimization=bool(resolved["post_optimization"]),
        compaction_threshold_gb=float(resolved["compaction_threshold_gb"]),
        peak_concurrency_override=resolved["peak_concurrency_override"],
        idle_hours_override=resolved["idle_hours_override"],
        source=source,
    )
