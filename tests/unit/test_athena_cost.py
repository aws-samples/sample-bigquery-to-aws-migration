"""Tests for Athena cost estimator."""
from __future__ import annotations

from decimal import Decimal

from bq_assess.engine.athena.cost import AthenaCostEstimator
from bq_assess.models import (
    BQPricingModel,
    ConfidenceLevel,
    PricingDetection,
    WorkloadProfile,
)


def _pricing() -> PricingDetection:
    return PricingDetection(
        model=BQPricingModel.ON_DEMAND,
        confidence=ConfidenceLevel.HIGH,
        source_note="test",
    )


def test_basic_on_demand_cost():
    profile = WorkloadProfile(
        has_data=True,
        monthly_scanned_tb=1.0,
        total_queries=1000,
        days_sampled=30,
        lookback_days=30,
        active_hour_fraction=0.2,
    )
    estimator = AthenaCostEstimator()
    result = estimator.estimate_cost(profile, _pricing())
    # 1 TB/month * $5/TB = $5/month compute
    assert result.engine_id == "athena"
    assert result.monthly_compute == Decimal(5)
    assert result.monthly_storage == Decimal(0)
    assert result.monthly_total == result.monthly_compute + result.monthly_storage
    assert result.pricing_mode == "on_demand"


def test_zero_scan_is_free():
    """Zero-scan workload (DDL-only) should get normal confidence, not 'no data' path."""
    profile = WorkloadProfile(
        has_data=True,
        monthly_scanned_tb=0.0,
        days_sampled=10,
        total_queries=50,
    )
    estimator = AthenaCostEstimator()
    result = estimator.estimate_cost(profile, _pricing())
    assert result.monthly_compute == Decimal(0)
    assert result.confidence == "MEDIUM"  # days_sampled >= 7
    assert "No workload data" not in result.source_note


def test_no_data_returns_zero():
    profile = WorkloadProfile(has_data=False)
    estimator = AthenaCostEstimator()
    result = estimator.estimate_cost(profile, _pricing())
    assert result.monthly_compute == Decimal(0)
    assert result.confidence == "LOW"


def test_ddl_is_free():
    """DDL queries cost nothing in Athena — only scanned bytes matter."""
    profile = WorkloadProfile(
        has_data=True,
        monthly_scanned_tb=0.0,
        total_queries=500,  # all DDL
    )
    estimator = AthenaCostEstimator()
    result = estimator.estimate_cost(profile, _pricing())
    assert result.monthly_compute == Decimal(0)


def test_optimize_one_time_cost():
    """One-time OPTIMIZE compaction cost is calculated from stored data."""
    profile = WorkloadProfile(
        has_data=True,
        total_stored_gb=1024.0,  # 1 TB
        monthly_scanned_tb=1.0,
        days_sampled=10,
        total_queries=100,
    )
    estimator = AthenaCostEstimator()
    result = estimator.estimate_cost(profile, _pricing())
    # 1024 GB / 1024 = 1 TB * $5/TB = $5
    assert result.one_time_migration == Decimal(5)
    assert "OPTIMIZE" in result.source_note or "compaction" in result.source_note


def test_unmodeled_charges_note():
    """Source note mentions unmodeled Glue and S3 charges."""
    profile = WorkloadProfile(
        has_data=True,
        monthly_scanned_tb=1.0,
        days_sampled=10,
    )
    estimator = AthenaCostEstimator()
    result = estimator.estimate_cost(profile, _pricing())
    assert "Glue catalog" in result.source_note or "Glue" in result.source_note
    assert "S3 request" in result.source_note or "S3" in result.source_note
    assert "not modeled" in result.source_note
