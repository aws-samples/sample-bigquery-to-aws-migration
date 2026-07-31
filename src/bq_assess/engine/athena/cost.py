"""Athena cost estimator — $5/TB scanned (on-demand) model.

Athena billing is simple: $5 per TB scanned. DDL and metadata queries are free.
10 MB minimum per query (confirmed for federated; assumed for S3-native).
No provisioned capacity mode is modeled in v1 (DPU reservations are Phase 2).
"""
from __future__ import annotations

from decimal import Decimal

from bq_assess.models import (
    EngineCostEstimate,
    PricingDetection,
    WorkloadProfile,
)

_ATHENA_USD_PER_TB = Decimal(5)
# S3 Tables managed compaction replaces the old self-managed one-time OPTIMIZE;
# it bills per GB processed (S3 pricing: Tables maintenance). Same $/TB order of
# magnitude as Athena OPTIMIZE scanning, kept as the initial-compaction estimate.
_S3TABLES_MAINTENANCE_USD_PER_TB = Decimal(5)  # one-time initial compaction


class AthenaCostEstimator:
    """Estimate monthly Athena cost from workload scan volume."""

    engine_id = "athena"

    def estimate_cost(
        self, profile: WorkloadProfile, pricing: PricingDetection
    ) -> EngineCostEstimate:
        if not profile.has_data:
            return EngineCostEstimate(
                engine_id=self.engine_id,
                monthly_total=Decimal(0),
                monthly_compute=Decimal(0),
                monthly_storage=Decimal(0),
                pricing_mode="on_demand",
                confidence="LOW",
                source_note="No workload data — Athena compute is $0 until queries run",
                one_time_migration=Decimal(0),
            )

        monthly_tb = Decimal(str(profile.monthly_scanned_tb))
        compute = monthly_tb * _ATHENA_USD_PER_TB

        # Storage is shared (S3 Tables) — not duplicated here; reported as $0 on the
        # Athena side since it's the same bucket as Redshift.
        storage = Decimal(0)

        # One-time initial compaction (S3 Tables managed maintenance) cost
        total_stored_tb = Decimal(str(profile.total_stored_gb)) / Decimal(1024)
        optimize_cost = total_stored_tb * _S3TABLES_MAINTENANCE_USD_PER_TB

        confidence = "MEDIUM" if profile.days_sampled >= 7 else "LOW"

        return EngineCostEstimate(
            engine_id=self.engine_id,
            monthly_total=compute + storage,
            monthly_compute=compute,
            monthly_storage=storage,
            pricing_mode="on_demand",
            confidence=confidence,
            source_note=(
                f"Athena on-demand @ ${_ATHENA_USD_PER_TB}/TB; "
                f"{monthly_tb:.2f} TB/month scanned. "
                f"One-time initial-compaction estimate (S3 Tables managed maintenance) is an upper bound — "
                f"based on uncompressed BigQuery logical size; "
                f"Parquet on S3 is typically several times smaller. "
                "Glue catalog requests and S3 request charges are not modeled."
            ),
            one_time_migration=optimize_cost,
        )
