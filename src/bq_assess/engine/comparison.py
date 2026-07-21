"""Unified cost comparison assembly — engine-aware (Fix 1 structural).

Assembles the CostComparison ONCE with full knowledge of both engines (Redshift
and Athena). Moves the Athena scenario building and the unified recommendation
logic out of cli.py so the estimator produces ALL derived fields in one pass —
no post-hoc dataclasses.replace patching.

This module sits next to the Redshift CostEstimator (engine/redshift/cost.py)
and the Athena CostEstimator (engine/athena/cost.py), bridging both.
"""
from __future__ import annotations

import dataclasses

from bq_assess.engine.redshift import cost_constants as k
from bq_assess.models import (
    AWSRecommendation,
    AWSScenario,
    ConfidenceLevel,
    CostComparison,
    CostLine,
    EngineCostEstimate,
    EngineRecommendation,
    WorkloadProfile,
)


# ================================================================== Public API


def assemble_cost_comparison(
    base_comparison: CostComparison,
    athena_estimate: EngineCostEstimate,
    workload_profile: WorkloadProfile,
    engine_recommendation: EngineRecommendation,
) -> CostComparison:
    """Assemble the unified cost comparison with all engines in ONE pass.

    Takes the Redshift-only base comparison from CostEstimator.estimate() and
    adds the Athena scenario, determines the unified recommendation, marks the
    recommended scenario, derives ALL fields from the winner, and returns the
    final CostComparison. No post-hoc patching needed.

    Args:
        base_comparison: Redshift-only CostComparison from CostEstimator.estimate()
        athena_estimate: EngineCostEstimate from AthenaCostEstimator.estimate_cost()
        workload_profile: WorkloadProfile built from slot data
        engine_recommendation: EngineRecommendation from RecommendationScorer
    """
    # Build Athena scenario (non-recommended initially)
    storage_line = _find_storage_line(base_comparison.aws_scenarios)
    athena_scenario = _build_athena_scenario(
        athena_estimate=athena_estimate,
        storage_line=storage_line,
        workload_profile=workload_profile,
    )

    # Combine all scenarios
    updated_scenarios = list(base_comparison.aws_scenarios) + [athena_scenario]

    # Generate unified recommendation considering engine scorer
    unified_recommendation = _generate_unified_recommendation(
        scenarios=updated_scenarios,
        workload_profile=workload_profile,
        bigquery_monthly=base_comparison.bigquery_monthly,
        engine_recommendation=engine_recommendation,
    )

    # Mark the recommended scenario
    for s in updated_scenarios:
        s.is_recommended = (s.label == unified_recommendation.recommended_scenario)
        if s.is_recommended:
            s.not_recommended_reason = ""

    # Rebuild the recommended scenario with elaborated justification
    recommended_label = unified_recommendation.recommended_scenario
    if "Athena" in recommended_label:
        athena_idx = next(i for i, s in enumerate(updated_scenarios) if s.label == recommended_label)
        updated_scenarios[athena_idx] = _build_athena_scenario(
            athena_estimate=athena_estimate,
            storage_line=storage_line,
            workload_profile=workload_profile,
            engine_recommendation=engine_recommendation,
            is_recommended=True,
        )
    else:
        # Redshift-recommended: rebuild with elaborated justification (Fix 5)
        rec_idx = next(
            (i for i, s in enumerate(updated_scenarios) if s.label == recommended_label),
            None,
        )
        if rec_idx is not None:
            scenario = updated_scenarios[rec_idx]
            justification = _build_redshift_elaborated_justification(
                workload_profile, engine_recommendation, scenario
            )
            updated_scenarios[rec_idx] = dataclasses.replace(
                scenario, justification=justification, is_recommended=True,
            )

    # Derive ALL fields from the winner in ONE pass
    best = next((s for s in updated_scenarios if s.is_recommended), updated_scenarios[0])

    aws_monthly_low = sum(_line_low(ln) for ln in best.lines)
    aws_monthly_high = sum(_line_high(ln) for ln in best.lines)
    bigquery_monthly = base_comparison.bigquery_monthly
    monthly_delta_low = bigquery_monthly - aws_monthly_high
    monthly_delta_high = bigquery_monthly - aws_monthly_low
    annual_savings_low = monthly_delta_low * 12
    annual_savings_high = monthly_delta_high * 12

    migration_onetime = base_comparison.migration_onetime
    breakeven_low = migration_onetime / monthly_delta_low if monthly_delta_low > 0 else k.BREAKEVEN_NEVER
    breakeven_high = migration_onetime / monthly_delta_high if monthly_delta_high > 0 else k.BREAKEVEN_NEVER

    # Estimate basis: derives from the winner (fixes #4)
    basis_level, basis_text = _derive_estimate_basis(
        base_comparison.bigquery_breakdown, best, workload_profile
    )

    # Order scenarios: recommended first, then ascending by monthly_total
    updated_scenarios.sort(key=lambda s: (not s.is_recommended, s.monthly_total))

    return dataclasses.replace(
        base_comparison,
        athena_one_time_optimize=athena_estimate.one_time_migration,
        aws_scenarios=updated_scenarios,
        recommendation=unified_recommendation,
        aws_lines=best.lines,
        aws_monthly_low=aws_monthly_low,
        aws_monthly_high=aws_monthly_high,
        monthly_delta_low=monthly_delta_low,
        monthly_delta_high=monthly_delta_high,
        annual_savings_low=annual_savings_low,
        annual_savings_high=annual_savings_high,
        breakeven_months_low=breakeven_low,
        breakeven_months_high=breakeven_high,
        compute_confidence=best.confidence,
        estimate_basis_level=basis_level,
        estimate_basis=basis_text,
    )


# ================================================================== Shared helpers


def _find_storage_line(scenarios: list[AWSScenario]) -> CostLine:
    """Extract the S3 storage line from existing Redshift scenarios (reuse for Athena)."""
    if not scenarios:
        return CostLine(
            label="S3 Tables storage",
            monthly=0.0,
            monthly_low=None,
            monthly_high=None,
            confidence=ConfidenceLevel.LOW,
            source_note="No storage data",
        )
    first_scenario = scenarios[0]
    for line in first_scenario.lines:
        if "storage" in line.label.lower():
            return line
    return CostLine(
        label="S3 Tables storage",
        monthly=0.0,
        monthly_low=None,
        monthly_high=None,
        confidence=ConfidenceLevel.LOW,
        source_note="Storage line not found in scenarios",
    )


# ================================================================== Athena scenario builder


def _build_athena_scenario(
    athena_estimate: EngineCostEstimate,
    storage_line: CostLine,
    workload_profile: WorkloadProfile,
    engine_recommendation: EngineRecommendation | None = None,
    is_recommended: bool = False,
) -> AWSScenario:
    """Build an AWSScenario for Athena from its cost estimate."""
    confidence_enum = (
        athena_estimate.confidence
        if isinstance(athena_estimate.confidence, ConfidenceLevel)
        else ConfidenceLevel(athena_estimate.confidence)
    )

    # Athena compute line
    compute_line = CostLine(
        label="Athena compute (on-demand $5/TB)",
        monthly=float(athena_estimate.monthly_compute),
        monthly_low=None,
        monthly_high=None,
        confidence=confidence_enum,
        source_note=athena_estimate.source_note,
    )

    # Justification
    qpd = workload_profile.queries_per_day if workload_profile.has_data else 0
    monthly_tb = workload_profile.monthly_scanned_tb if workload_profile.has_data else 0
    if workload_profile.has_data and is_recommended and engine_recommendation:
        justification = _build_athena_elaborated_justification(
            workload_profile, engine_recommendation, athena_estimate
        )
    elif workload_profile.has_data:
        justification = (
            f"Your workload scans {monthly_tb:.2f} TB/month ({qpd:,.0f} queries/day). "
            f"Athena's pay-per-scan model is cost-effective at low to moderate scan volumes. "
            f"One-time OPTIMIZE compaction: ~${float(athena_estimate.one_time_migration):,.2f} (upper bound)."
        )
    else:
        justification = "No workload data — Athena compute is $0 until queries run."

    lines = [compute_line, storage_line]
    monthly_total = float(athena_estimate.monthly_compute) + (storage_line.monthly or 0)

    return AWSScenario(
        label="Athena (on-demand $5/TB)",
        category="ATHENA_ONDEMAND",
        lines=lines,
        monthly_total=monthly_total,
        confidence=confidence_enum,
        is_recommended=is_recommended,
        justification=justification,
        cluster_config="",
        workload_fit_notes=[],
        not_recommended_reason="",
    )


# ================================================================== Justification builders


def _activity_pattern_label(active_fraction: float) -> str:
    """Shared helper: classify activity pattern from active_hour_fraction."""
    if active_fraction < 0.3:
        return "intermittent"
    elif active_fraction < 0.6:
        return "moderate"
    return "sustained"


def _crossover_assumptions_sentence(crossover: float) -> str:
    """Shared helper: the crossover context sentence both justifications use."""
    return (
        "The crossover assumes Redshift's minimum posture "
        "(4 RPU, ~8 active hours/day); always-on workloads break even nearer 8 TB/day. "
        "Note: if the workgroup auto-scales above 4 RPUs it will not scale back down "
        "automatically — the 4-RPU floor requires manual reset, so sustained bursts "
        "erode this posture. "
    )


def _revisit_conditions_sentence(crossover: float) -> str:
    """Shared helper: the revisit-if conditions both justifications reference."""
    return (
        f"Revisit if scan volume approaches {crossover:.2f} TB/day, sustained concurrency "
        f"approaches your account's Athena DML quota (default 100 in ap-southeast-2, adjustable), "
        f"or sub-3-second latency SLAs emerge. "
    )


def _build_athena_elaborated_justification(
    workload_profile: WorkloadProfile,
    engine_recommendation: EngineRecommendation,
    athena_estimate: EngineCostEstimate,
) -> str:
    """Elaborated justification for recommended Athena scenario (R19 unified surface).

    Composes 3-5 sentences: scan volume vs crossover, pattern fit, confidence inline,
    revisit conditions, one-time OPTIMIZE line.
    """
    qpd = workload_profile.queries_per_day
    monthly_tb = workload_profile.monthly_scanned_tb
    daily_tb = monthly_tb / 30
    crossover = float(engine_recommendation.crossover_point_tb_day)
    peak_conc = workload_profile.peak_concurrent_queries or workload_profile.avg_concurrent_queries or 5

    # Fix 6: override_reason handling — do NOT claim signal-based analysis for overrides
    if engine_recommendation.override_reason:
        justification = (
            f"This Query Engine was selected by explicit override: "
            f"{engine_recommendation.override_reason}. "
        )
        justification += (
            f"Your workload scans {monthly_tb:.2f} TB/month ({daily_tb:.2f} TB/day, {qpd:,.0f} queries/day). "
            f"One-time OPTIMIZE compaction: ~${float(athena_estimate.one_time_migration):,.2f} (upper bound)."
        )
        return justification

    confidence_pct = int(engine_recommendation.confidence * 100)

    # Core justification: volume, crossover, pattern
    justification = (
        f"Your workload scans {monthly_tb:.2f} TB/month ({daily_tb:.2f} TB/day, {qpd:,.0f} queries/day) — "
        f"far below the ~{crossover:.2f} TB/day crossover where Redshift Serverless becomes cheaper. "
    )
    justification += _crossover_assumptions_sentence(crossover)

    # Pattern fit
    conc_phrase = (
        "Peak concurrency under 1 concurrent query"
        if peak_conc < 1
        else f"Peak concurrency of {peak_conc:.0f} concurrent queries"
    )
    pattern = _activity_pattern_label(workload_profile.active_hour_fraction)
    justification += (
        f"{conc_phrase} and an {pattern} pattern suit per-query billing — "
        f"neither engine bills between queries; Athena avoids resume minimums and "
        f"keep-alive charges. "
    )

    # Confidence inline
    justification += f"Engine analysis: {confidence_pct}% confidence. "

    # Revisit conditions
    justification += _revisit_conditions_sentence(crossover)

    # One-time OPTIMIZE
    justification += (
        f"One-time OPTIMIZE compaction: ~${float(athena_estimate.one_time_migration):,.2f} (upper bound)."
    )

    return justification


def _build_redshift_elaborated_justification(
    workload_profile: WorkloadProfile,
    engine_recommendation: EngineRecommendation,
    scenario: AWSScenario,
) -> str:
    """Elaborated justification for recommended Redshift scenario (Fix 5).

    Composes 3-5 sentences: scan volume vs crossover, pattern fit, confidence inline,
    revisit conditions, warmth caveat if present.
    """
    qpd = workload_profile.queries_per_day
    monthly_tb = workload_profile.monthly_scanned_tb
    daily_tb = monthly_tb / 30 if monthly_tb else 0
    crossover = float(engine_recommendation.crossover_point_tb_day)
    active_frac = workload_profile.active_hour_fraction

    # Fix 6: override_reason handling
    if engine_recommendation.override_reason:
        justification = (
            f"This Query Engine was selected by explicit override: "
            f"{engine_recommendation.override_reason}. "
        )
        justification += (
            f"Your workload scans {monthly_tb:.2f} TB/month ({qpd:,.0f} queries/day). "
            f"Redshift's always-on compute provides sub-second latency for sustained workloads."
        )
        return justification

    confidence_pct = int(engine_recommendation.confidence * 100)
    pattern = _activity_pattern_label(active_frac)

    # Core justification: volume, crossover, pattern
    justification = (
        f"Your workload scans {monthly_tb:.2f} TB/month ({daily_tb:.2f} TB/day, "
        f"{qpd:,.0f} queries/day) — "
    )
    if daily_tb >= crossover:
        justification += (
            f"above the ~{crossover:.2f} TB/day crossover where Redshift becomes cheaper than Athena. "
        )
    else:
        justification += (
            f"below the ~{crossover:.2f} TB/day crossover, but concurrency and pattern signals "
            f"favor Redshift. "
        )
    justification += _crossover_assumptions_sentence(crossover)

    # Pattern fit
    peak_conc = workload_profile.peak_concurrent_queries or workload_profile.avg_concurrent_queries or 5
    justification += (
        f"Peak concurrency of {peak_conc:.0f} concurrent queries and a {pattern} "
        f"pattern (active {active_frac:.0%} of hours) suit Redshift's per-second billing — "
        f"neither engine bills between queries; Redshift avoids resume minimums and "
        f"keep-alive charges at this activity level. "
    )

    # Confidence inline
    justification += f"Engine analysis: {confidence_pct}% confidence. "

    # Revisit conditions
    justification += _revisit_conditions_sentence(crossover)

    # Warmth caveat (check for sla_warmth_caveat signal)
    for sig in engine_recommendation.reasoning:
        if sig.signal == "sla_warmth_caveat":
            justification += (
                f"Note: Sub-3s SLA requested but workload is idle {sig.value:.0%} of time — "
                f"a suspended Redshift Serverless workgroup resumes in ~30s; neither engine "
                f"guarantees sub-3s on a cold path."
            )
            break

    return justification


# ================================================================== Unified recommendation


def _generate_unified_recommendation(
    scenarios: list[AWSScenario],
    workload_profile: WorkloadProfile,
    bigquery_monthly: float,
    engine_recommendation: EngineRecommendation,
) -> AWSRecommendation:
    """Generate unified recommendation considering both cost and engine signals.

    Decision logic:
    - If engine scorer says "athena" and Athena is cheapest or within 20% of cheapest:
      -> Recommend Athena; demote others with "Engine analysis recommends Athena"
    - If engine scorer says "redshift":
      -> Recommend cheapest Redshift scenario; demote Athena with signal reasons
    - If there's tension (scorer says X, but Y is far cheaper):
      -> Banner notes the tension in one sentence
    """
    athena_scenarios = [s for s in scenarios if "Athena" in s.label]
    redshift_scenarios = [s for s in scenarios if "Athena" not in s.label]

    if not athena_scenarios:
        cheapest = min(redshift_scenarios, key=lambda s: s.monthly_total)
        return AWSRecommendation(
            recommended_scenario=cheapest.label,
            reasoning=_build_redshift_reasoning(cheapest, workload_profile, bigquery_monthly, redshift_scenarios),
            workload_profile=workload_profile,
            alternatives_considered=[s.label for s in scenarios if s.label != cheapest.label],
        )

    athena = athena_scenarios[0]
    cheapest_redshift = min(redshift_scenarios, key=lambda s: s.monthly_total) if redshift_scenarios else None

    if not cheapest_redshift:
        return AWSRecommendation(
            recommended_scenario=athena.label,
            reasoning="Athena is the only evaluated option.",
            workload_profile=workload_profile,
            alternatives_considered=[],
        )

    cheapest_overall = min(scenarios, key=lambda s: s.monthly_total)
    # MRI-2b: absolute floor — at sub-$10 scale treat all scenarios as cost-equivalent
    # and let the scorer decide explicitly.
    costs_negligible = cheapest_overall.monthly_total < 10.0 and athena.monthly_total < 10.0
    athena_within_20pct = (
        athena.monthly_total <= cheapest_overall.monthly_total * 1.20
        or costs_negligible
    )

    primary_engine = engine_recommendation.primary_engine.lower()

    if primary_engine == "athena" and athena_within_20pct:
        if costs_negligible:
            reasoning = _build_athena_reasoning(athena, workload_profile, engine_recommendation, negligible=True)
        else:
            reasoning = _build_athena_reasoning(athena, workload_profile, engine_recommendation)
        for s in redshift_scenarios:
            if not s.not_recommended_reason:
                s.not_recommended_reason = "Engine analysis recommends Athena for this workload shape"
        return AWSRecommendation(
            recommended_scenario=athena.label,
            reasoning=reasoning,
            workload_profile=workload_profile,
            alternatives_considered=[s.label for s in scenarios if s.label != athena.label],
        )
    elif primary_engine == "redshift":
        reasoning = _build_redshift_reasoning(cheapest_redshift, workload_profile, bigquery_monthly, redshift_scenarios)
        if athena.monthly_total < cheapest_redshift.monthly_total * 0.5:
            reasoning = (
                f"Engine analysis favors Redshift (high concurrency/volume signals); "
                f"note Athena is cheaper at current volumes (${athena.monthly_total:,.2f}/mo vs "
                f"${cheapest_redshift.monthly_total:,.2f}/mo) — signal detail in JSON export. "
                + reasoning
            )
        signal_reasons = _extract_signal_reasons(engine_recommendation)
        athena.not_recommended_reason = f"Workload signals favor Redshift: {signal_reasons}"
        return AWSRecommendation(
            recommended_scenario=cheapest_redshift.label,
            reasoning=reasoning,
            workload_profile=workload_profile,
            alternatives_considered=[s.label for s in scenarios if s.label != cheapest_redshift.label],
        )
    else:
        # Tension: scorer says athena but Athena fails the 20% gate — recommend cheapest
        cheapest_label = cheapest_overall.label
        pct = round((1 - cheapest_overall.monthly_total / max(athena.monthly_total, 0.01)) * 100)
        reasoning = (
            f"Engine signal analysis favored Athena, but at your volumes "
            f"{cheapest_label} is {pct}% cheaper — cost decides here. "
            f"Revisit Athena if volumes drop."
        )
        return AWSRecommendation(
            recommended_scenario=cheapest_label,
            reasoning=reasoning,
            workload_profile=workload_profile,
            alternatives_considered=[s.label for s in scenarios if s.label != cheapest_label],
        )


# ================================================================== Estimate basis


def _derive_estimate_basis(
    bq_breakdown: list[CostLine],
    best: AWSScenario,
    workload_profile: WorkloadProfile,
) -> tuple[ConfidenceLevel, str]:
    """Derive the estimate_basis from the winning scenario (Fix 4).

    When Athena is the winner, the sentence names Athena's pricing model.
    When Redshift is the winner, the sentence names Redshift's pricing inputs.
    Level = minimum across the BQ breakdown and the recommended option's lines.
    """
    _CONFIDENCE_ORDER = {
        ConfidenceLevel.LOW: 0,
        ConfidenceLevel.MEDIUM: 1,
        ConfidenceLevel.HIGH: 2,
    }
    lines = list(bq_breakdown) + list(best.lines)
    level = min(
        (ln.confidence for ln in lines),
        key=lambda c: _CONFIDENCE_ORDER.get(c, 0),
        default=ConfidenceLevel.LOW,
    )

    is_athena = "Athena" in best.label

    if is_athena:
        if workload_profile.has_data:
            monthly_tb = workload_profile.monthly_scanned_tb
            text = (
                f"Priced from Athena on-demand ($5/TB scanned); "
                f"projected {monthly_tb:.2f} TB/month from measured workload data "
                f"and current published rates."
            )
        else:
            text = (
                "Priced from Athena on-demand ($5/TB scanned); no workload data — "
                "compute is $0 until queries run."
            )
    else:
        if workload_profile.has_data:
            text = (
                "Priced from Redshift Serverless RPU-hours; "
                "projected from measured workload data and current published rates."
            )
        else:
            text = (
                "No workload data — compute is a rough range. "
                "Provide query logs for a measured estimate."
            )

    return level, text


# ================================================================== Reasoning helpers


def _build_athena_reasoning(
    athena_scenario: AWSScenario,
    workload_profile: WorkloadProfile,
    engine_recommendation: EngineRecommendation,
    negligible: bool = False,
) -> str:
    """Build reasoning for Athena recommendation."""
    qpd = workload_profile.queries_per_day
    monthly_tb = workload_profile.monthly_scanned_tb
    confidence_pct = int(engine_recommendation.confidence * 100)
    if negligible:
        return (
            f"Recommended Query Engine: Athena (on-demand $5/TB). "
            f"Costs are negligible at this scale (<$10/mo) — the recommendation is signal-based. "
            f"Engine analysis ({confidence_pct}% confidence) favors Athena's pay-per-scan model "
            f"for this scan volume and query pattern. Signal detail available in the JSON export."
        )
    return (
        f"Recommended Query Engine: Athena (on-demand $5/TB). "
        f"Your workload scans {monthly_tb:.2f} TB/month ({qpd:,.0f} queries/day). "
        f"Engine analysis ({confidence_pct}% confidence) favors Athena's pay-per-scan model "
        f"for this scan volume and query pattern. Signal detail available in the JSON export."
    )


def _build_redshift_reasoning(
    scenario: AWSScenario,
    workload_profile: WorkloadProfile,
    bq_monthly: float,
    all_redshift: list[AWSScenario],
) -> str:
    """Build reasoning for Redshift recommendation."""
    qpd = workload_profile.queries_per_day
    monthly_tb = workload_profile.monthly_scanned_tb
    active_frac = workload_profile.active_hour_fraction

    if "Serverless" in scenario.label:
        return (
            f"Recommended Query Engine: Redshift {scenario.label}. "
            f"Your workload runs {qpd:,.0f} queries/day (active {active_frac:.0%} of hours). "
            f"Serverless auto-scales to zero during idle periods and handles burst without pre-provisioning."
        )
    else:
        return (
            f"Recommended Query Engine: Redshift {scenario.label}. "
            f"Your workload runs {qpd:,.0f} queries/day scanning {monthly_tb:,.0f} TB/month. "
            f"This is a sustained, high-volume pattern (active {active_frac:.0%} of hours). "
            f"Provisioned RG with a committed term saves significantly vs on-demand."
        )


def _extract_signal_reasons(engine_recommendation: EngineRecommendation) -> str:
    """Extract the top signal reasons from engine recommendation."""
    top_signals = sorted(
        engine_recommendation.reasoning,
        key=lambda sig: abs(sig.weight),
        reverse=True,
    )[:2]
    reasons = [sig.signal.replace("_", " ") for sig in top_signals if sig.direction == "redshift"]
    return ", ".join(reasons) if reasons else "high concurrency/volume"


# ================================================================== Line value helpers


def _line_low(line: CostLine) -> float:
    return line.monthly if line.monthly is not None else (line.monthly_low or 0)


def _line_high(line: CostLine) -> float:
    return line.monthly if line.monthly is not None else (line.monthly_high or 0)
