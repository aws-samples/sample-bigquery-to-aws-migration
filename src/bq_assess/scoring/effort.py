"""Migration Effort scorer (R9) — Tables only.

Factors: data-volume tier (dominant), lossy-cast count, ongoing-sync need,
non-clean partition/sort mapping. Nesting and clean partitioning contribute
zero (R9.3, ADR-0002).
"""
from __future__ import annotations

from bq_assess.core.units import bytes_to_gb, fmt_size
from bq_assess.models import (
    ConfidenceLevel,
    ConversionResult,
    EffortCategory,
    EffortResult,
    EntityMetadata,
)

_ONE_GB = 1024**3
_LARGE_THRESHOLD = 1 * _ONE_GB
_HUGE_THRESHOLD = 100 * _ONE_GB

_SYNC_SIGNALS = {"_partitiontime", "updated_at", "modified_at", "ingestion_time"}


def _category_for(points: int) -> EffortCategory:
    """Single points→category ladder shared by initial scoring and re-scoring."""
    if points == 0:
        return EffortCategory.AUTO
    if points <= 2:
        return EffortCategory.ASSISTED
    return EffortCategory.MANUAL


class EffortScorer:
    """Score migration effort for TABLE-population entities."""

    def score(self, entity: EntityMetadata, conversion: ConversionResult) -> EffortResult:
        if not conversion.success:
            return EffortResult(
                category=EffortCategory.MANUAL,
                score=99,
                flags=["conversion_failed"],
                reasoning="Iceberg DDL conversion failed — manual migration design required.",
                confidence=ConfidenceLevel.HIGH,
            )

        points = 0
        flags: list[str] = []
        reasons: list[str] = []

        # Data-volume tier (dominant factor)
        size_bytes = entity.num_bytes or 0
        if size_bytes >= _HUGE_THRESHOLD:
            points += 2
            flags.append("data_volume_huge")
            reasons.append(
                f"huge volume ({fmt_size(bytes_to_gb(size_bytes))}) — partition-wise load (+2)"
            )
        elif size_bytes >= _LARGE_THRESHOLD:
            points += 1
            flags.append("data_volume_large")
            reasons.append(
                f"large volume ({fmt_size(bytes_to_gb(size_bytes))}) — staged COPY (+1)"
            )

        # Lossy casts
        n_lossy = len(conversion.lossy_casts)
        if n_lossy > 0:
            points += n_lossy
            flags.append("lossy_casts")
            reasons.append(f"{n_lossy} lossy cast(s) — manual type review (+{n_lossy})")

        # Ongoing-sync need
        if self._has_sync_signal(entity):
            points += 1
            flags.append("ongoing_sync")
            reasons.append("ongoing-sync signal detected — recurring MERGE needed (+1)")

        # Non-clean partition/sort — the converter marks these via auto_derived=False
        # with prose decision_flags; one point per flagged decision (R9).
        pm = conversion.partition_mapping
        if pm is not None and not pm.auto_derived and pm.decision_flags:
            n_decisions = len(pm.decision_flags)
            points += n_decisions
            flags.append("partition_decision_required")
            reasons.append(
                f"{n_decisions} partition/sort decision(s) need review (+{n_decisions})"
            )

        category = _category_for(points)

        # Confidence
        if entity.num_bytes is None:
            confidence = ConfidenceLevel.LOW
        else:
            confidence = ConfidenceLevel.HIGH

        return EffortResult(
            category=category,
            score=points,
            flags=flags,
            reasoning="; ".join(reasons) if reasons else "No effort factors detected — fully automatable.",
            confidence=confidence,
        )

    def _has_sync_signal(self, entity: EntityMetadata) -> bool:
        if entity.time_partitioning:
            field = entity.time_partitioning.field or ""
            if field and (field.upper() == "_PARTITIONTIME" or field.lower() in _SYNC_SIGNALS):
                return True
        for col in entity.columns:
            if col.name and col.name.lower() in _SYNC_SIGNALS:
                return True
        return False


def amend_for_rms_placement(result: EffortResult) -> EffortResult:
    """Re-score an entity whose Storage Target is RMS (ADR-0005).

    RMS entities carry a two-phase load (Athena→Iceberg staging, then Redshift
    CREATE TABLE + INSERT…SELECT + staging cleanup) — one extra coordinated step
    over the single-phase Iceberg path. +1 point, re-derive the category with the
    same thresholds as EffortScorer.score().

    Called after the storage-placement stage; effort itself is scored earlier
    (Stage 5), before the engine recommendation exists.
    """
    points = result.score + 1
    return EffortResult(
        category=_category_for(points),
        score=points,
        flags=[*result.flags, "rms_two_phase_load"],
        reasoning=(
            f"{result.reasoning}; RMS storage placement — two-phase load "
            f"(Iceberg staging → Redshift native) (+1)"
        ),
        confidence=result.confidence,
    )
