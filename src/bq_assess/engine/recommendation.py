"""Engine Recommendation Scorer — 8-signal decision between Athena and Redshift.

Uses workload signals derived from BQ INFORMATION_SCHEMA.JOBS to recommend
the primary query engine. Athena wins for low-volume, spiky, fragmented-activity
workloads. Redshift wins for high-volume, steady, latency-sensitive workloads.

Both engines bill zero when idle (verified); the idle_ratio signal captures
residual edges: SLS 60s-minimum per resume and billable pool keep-alives favor
Athena for fragmented patterns; sustained activity favors SLS warm-cluster
latency consistency.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List

from bq_assess.models import (
    EngineConfig,
    EngineRecommendation,
    SignalContribution,
    WorkloadProfile,
)

# Crossover derivation: cheapest Redshift posture = 4 RPU minimum x 8 active hours/day.
# us-east-1: (0.375*4*8)/5 = 2.4 TB/day; ap-southeast-2: (0.419*4*8)/5 = 2.68 ~ 2.7.
# Real crossover drops below 2 TB/day when RPUs auto-scale above the minimum.
# Note: once the workgroup scales above 4 RPUs it will not scale back down
# automatically — the 4-RPU floor requires manual reset (possible while <32TB storage).
_CROSSOVER_TB_DAY_SYDNEY = Decimal("2.7")
_CROSSOVER_TB_DAY_US = Decimal("2.4")
_CROSSOVER_DEFAULT = Decimal("2.5")

_SLA_COLD_START_THRESHOLD_MS = 3000
_IDLE_RATIO_WARMTH_THRESHOLD = 0.3  # below this, cluster is warm enough for SLA override


class RecommendationScorer:
    """Score Athena vs Redshift using 8 workload signals."""

    def recommend(
        self, profile: WorkloadProfile, config: EngineConfig
    ) -> EngineRecommendation:
        if config.preferred_engine:
            return EngineRecommendation(
                primary_engine=config.preferred_engine,
                confidence=1.0,
                reasoning=[],
                crossover_point_tb_day=self._crossover(config.target_region),
                override_reason=f"User selected {config.preferred_engine}",
            )

        if config.query_sla_ms < _SLA_COLD_START_THRESHOLD_MS:
            # SLA override is only valid if the cluster stays warm. Redshift Serverless
            # auto-suspends when idle and takes ~30s to resume (vs Athena 1-3s startup).
            # Compute idle ratio from the profile (same derivation as _score_signals).
            idle_ratio = 1.0 - profile.active_hour_fraction if profile.has_data else 0.0

            if not profile.has_data or idle_ratio < _IDLE_RATIO_WARMTH_THRESHOLD:
                # Warm cluster: keep Redshift override (sub-second warm latency)
                return EngineRecommendation(
                    primary_engine="redshift",
                    confidence=0.95,
                    reasoning=[
                        SignalContribution(
                            signal="interactive_ratio",
                            value=1.0,
                            direction="redshift",
                            weight=1.0,
                        )
                    ],
                    crossover_point_tb_day=self._crossover(config.target_region),
                    override_reason=(
                        f"Query SLA {config.query_sla_ms}ms < {_SLA_COLD_START_THRESHOLD_MS}ms — "
                        "Athena cold start (1-3s) exceeds target latency"
                    ),
                )
            # Idle cluster: fall through to normal scoring but add a warmth caveat
            # The caveat is encoded as a neutral signal contribution with weight 0.0

        if not profile.has_data:
            return EngineRecommendation(
                primary_engine="athena",
                confidence=0.6,
                reasoning=[],
                crossover_point_tb_day=self._crossover(config.target_region),
                override_reason=None,
            )

        contributions = self._score_signals(profile, config)

        # If SLA < 3000ms but cluster is idle-heavy, add a warmth caveat
        if (config.query_sla_ms < _SLA_COLD_START_THRESHOLD_MS and
                profile.has_data and
                (1.0 - profile.active_hour_fraction) >= _IDLE_RATIO_WARMTH_THRESHOLD):
            idle_ratio = 1.0 - profile.active_hour_fraction
            contributions.append(
                SignalContribution(
                    signal="sla_warmth_caveat",
                    value=idle_ratio,
                    direction="neutral",
                    weight=0.0,
                )
            )

        athena_score = sum(
            c.weight for c in contributions if c.direction == "athena"
        )
        redshift_score = sum(
            c.weight for c in contributions if c.direction == "redshift"
        )
        total = athena_score + redshift_score
        if total == 0:
            confidence = 0.5
            primary = "athena"
        else:
            confidence = max(athena_score, redshift_score) / total
            primary = "athena" if athena_score >= redshift_score else "redshift"

        return EngineRecommendation(
            primary_engine=primary,
            confidence=round(confidence, 2),
            reasoning=contributions,
            crossover_point_tb_day=self._crossover(config.target_region),
            override_reason=None,
        )

    def _score_signals(
        self, profile: WorkloadProfile, config: EngineConfig
    ) -> List[SignalContribution]:
        contributions: List[SignalContribution] = []
        crossover = float(self._crossover(config.target_region))

        # Check if SLA warmth caveat is active (sub-3s SLA + idle cluster)
        idle_ratio = 1.0 - profile.active_hour_fraction
        warmth_caveat_active = (
            config.query_sla_ms < _SLA_COLD_START_THRESHOLD_MS and
            idle_ratio >= _IDLE_RATIO_WARMTH_THRESHOLD
        )

        daily_tb = profile.monthly_scanned_tb / 30 if profile.monthly_scanned_tb else 0
        if daily_tb < crossover:
            contributions.append(SignalContribution(
                signal="daily_scan_volume", value=daily_tb,
                direction="athena", weight=0.30,
            ))
        else:
            contributions.append(SignalContribution(
                signal="daily_scan_volume", value=daily_tb,
                direction="redshift", weight=0.30,
            ))

        # Fragmented-activity signal (HRI-2 reframe): both engines bill zero when
        # idle (verified). This captures residual edges: SLS 60s-minimum per resume
        # and billable pool keep-alives favor Athena for fragmented patterns;
        # sustained activity favors SLS warm-cluster latency consistency.
        idle_ratio = 1.0 - profile.active_hour_fraction
        if idle_ratio > 0.5:
            contributions.append(SignalContribution(
                signal="idle_ratio", value=idle_ratio,
                direction="athena", weight=0.05,
            ))
        elif idle_ratio < 0.3:
            contributions.append(SignalContribution(
                signal="idle_ratio", value=idle_ratio,
                direction="redshift", weight=0.05,
            ))
        else:
            contributions.append(SignalContribution(
                signal="idle_ratio", value=idle_ratio,
                direction="neutral", weight=0.0,
            ))

        # Burstiness is undefined without meaningful average load
        avg_slots = profile.avg_slots
        if avg_slots < 0.01:
            # No meaningful workload → burstiness signal is neutral
            contributions.append(SignalContribution(
                signal="burstiness_cv", value=0.0,
                direction="neutral", weight=0.0,
            ))
        else:
            peak_slots = profile.peak_slots or avg_slots
            burstiness = peak_slots / avg_slots
            if burstiness > 1.5:
                contributions.append(SignalContribution(
                    signal="burstiness_cv", value=burstiness,
                    direction="athena", weight=0.05,
                ))
            elif burstiness < 0.8:
                contributions.append(SignalContribution(
                    signal="burstiness_cv", value=burstiness,
                    direction="redshift", weight=0.05,
                ))
            else:
                contributions.append(SignalContribution(
                    signal="burstiness_cv", value=burstiness,
                    direction="neutral", weight=0.0,
                ))

        # Concurrency signal: >50 threshold is a heuristic midpoint of the default
        # Athena DML quota (100 in ap-southeast-2, adjustable); not an AWS-published
        # break-even point.
        peak_conc = profile.peak_concurrent_queries or 0
        if peak_conc < 20:
            contributions.append(SignalContribution(
                signal="peak_concurrency", value=peak_conc,
                direction="athena", weight=0.20,
            ))
        elif peak_conc > 50:
            contributions.append(SignalContribution(
                signal="peak_concurrency", value=peak_conc,
                direction="redshift", weight=0.20,
            ))
        else:
            contributions.append(SignalContribution(
                signal="peak_concurrency", value=peak_conc,
                direction="neutral", weight=0.0,
            ))

        if profile.total_slot_ms and profile.monthly_scanned_tb:
            scanned_bytes = profile.monthly_scanned_tb * (1024**4)
            intensity = profile.total_slot_ms / max(scanned_bytes, 1)
            if intensity < 0.001:
                contributions.append(SignalContribution(
                    signal="compute_intensity", value=intensity,
                    direction="athena", weight=0.20,
                ))
            else:
                contributions.append(SignalContribution(
                    signal="compute_intensity", value=intensity,
                    direction="redshift", weight=0.20,
                ))
        else:
            contributions.append(SignalContribution(
                signal="compute_intensity", value=0,
                direction="neutral", weight=0.0,
            ))

        contributions.append(SignalContribution(
            signal="cache_hit_rate", value=0,
            direction="neutral", weight=0.0,
        ))

        # Interactive signal: suppress when warmth caveat is active — the model must
        # not push toward the engine its own caveat warns against
        if config.query_sla_ms < 5000 and not warmth_caveat_active:
            contributions.append(SignalContribution(
                signal="interactive_ratio", value=config.query_sla_ms,
                direction="redshift", weight=0.15,
            ))
        else:
            contributions.append(SignalContribution(
                signal="interactive_ratio", value=config.query_sla_ms,
                direction="neutral", weight=0.0,
            ))

        qpd = profile.queries_per_day or 0
        if qpd > 100 and daily_tb < 1.0:
            contributions.append(SignalContribution(
                signal="distinct_users", value=qpd,
                direction="athena", weight=0.05,
            ))
        else:
            contributions.append(SignalContribution(
                signal="distinct_users", value=qpd,
                direction="neutral", weight=0.0,
            ))

        return contributions

    def _crossover(self, region: str) -> Decimal:
        if "sydney" in region.lower() or "ap-southeast-2" in region:
            return _CROSSOVER_TB_DAY_SYDNEY
        if "us-east" in region:
            return _CROSSOVER_TB_DAY_US
        return _CROSSOVER_DEFAULT
