# Review fixes (2026-07-03): live price lookup parsing regressions.
"""Unit tests for PriceLookup parsing — S3 Tables per-dimension tier prices and
the zero-SKU GCP result treated as a miss (not a fake-live result)."""

from __future__ import annotations

import time

import pytest

from bq_assess.core.price_lookup import (
    PriceLookup,
    PricingRates,
    PricingTimeout,
    fetch_live_rates_with_timeout,
)


def _offer(products, terms):
    return products, {"OnDemand": terms}["OnDemand"]


def test_s3_tables_tiers_read_each_dimensions_own_price() -> None:
    """One SKU carries all three tiers as priceDimensions with DIFFERENT prices —
    each tier must get its own, not the first dimension's price three times."""
    products = {
        "SKU1": {"attributes": {"usagetype": "Tables-TimedStorage-ByteHrs"}},
    }
    terms = {
        "SKU1": {
            "offer": {
                "priceDimensions": {
                    "d1": {"beginRange": "0", "endRange": "51200",
                           "pricePerUnit": {"USD": "0.0265"}},
                    "d2": {"beginRange": "51200", "endRange": "512000",
                           "pricePerUnit": {"USD": "0.0253"}},
                    "d3": {"beginRange": "512000", "endRange": "Inf",
                           "pricePerUnit": {"USD": "0.0242"}},
                }
            }
        }
    }
    tiers = PriceLookup._parse_s3_tables_tiers(products, terms)
    assert tiers == {"tier1": 0.0265, "tier2": 0.0253, "tier3": 0.0242}


def test_s3_tables_ignores_intelligent_tiering_variants() -> None:
    """INT-FA/INT-IA usagetypes must not clobber the Standard tiers."""
    products = {
        "STD": {"attributes": {"usagetype": "APS2-Tables-TimedStorage-ByteHrs"}},
        "INT": {"attributes": {"usagetype": "APS2-Tables-TimedStorage-INT-FA-ByteHrs"}},
    }
    terms = {
        "STD": {"o": {"priceDimensions": {"d": {"beginRange": "0", "pricePerUnit": {"USD": "0.0288"}}}}},
        "INT": {"o": {"priceDimensions": {"d": {"beginRange": "0", "pricePerUnit": {"USD": "0.9999"}}}}},
    }
    tiers = PriceLookup._parse_s3_tables_tiers(products, terms)
    assert tiers == {"tier1": 0.0288}


def test_gcp_parse_with_no_matching_region_returns_zeroed_rates() -> None:
    """A region-filtered parse that matches nothing yields zero rates — the caller
    (_fetch_gcp_rates) must then treat it as a miss and fall back to hardcoded."""
    lookup = PriceLookup(aws_region="us-east-1", bq_location="mars-north1", use_cache=False)
    rates = lookup._parse_gcp_skus([
        {
            "description": "Analysis (australia-southeast1)",
            "serviceRegions": ["australia-southeast1"],
            "category": {"resourceGroup": "OnDemandAnalysis"},
            "pricingInfo": [{"pricingExpression": {
                "usageUnit": "TiBy",
                "tieredRates": [{"unitPrice": {"units": "8", "nanos": 125000000}}],
            }}],
        }
    ])
    assert rates.ondemand_usd_per_tib == 0    # region filter excluded the only SKU


def test_gcp_parse_matches_own_region() -> None:
    lookup = PriceLookup(aws_region="ap-southeast-2", bq_location="australia-southeast1",
                         use_cache=False)
    rates = lookup._parse_gcp_skus([
        {
            "description": "Analysis (australia-southeast1)",
            "serviceRegions": ["australia-southeast1"],
            "category": {"resourceGroup": "OnDemandAnalysis"},
            "pricingInfo": [{"pricingExpression": {
                "usageUnit": "TiBy",
                "tieredRates": [{"unitPrice": {"units": "8", "nanos": 125000000}}],
            }}],
        }
    ])
    assert rates.ondemand_usd_per_tib == 8.125


def test_mixed_live_hardcoded_result_is_not_cached(tmp_path, monkeypatch) -> None:
    """AWS-live + GCP-fallback must NOT be cached — a 24h entry would pin the fallback
    and suppress the GCP retry after a transient failure clears."""
    import bq_assess.core.price_lookup as pl
    monkeypatch.setattr(pl, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(pl, "_CACHE_FILE", tmp_path / "pricing-cache.json")

    lookup = PriceLookup(aws_region="us-east-1", bq_location="us", use_cache=True)
    monkeypatch.setattr(lookup, "_fetch_aws_rates", lambda: pl.AWSRates(
        rpu_hour_usd=0.375, fetched_at="2026-07-04", source="AWS Price List API"))
    rates = lookup.fetch(gcp_client=None)   # no client → GCP falls back to hardcoded
    assert rates.gcp.source.startswith("hardcoded")
    assert not (tmp_path / "pricing-cache.json").exists()


def test_both_live_result_is_cached(tmp_path, monkeypatch) -> None:
    import bq_assess.core.price_lookup as pl
    monkeypatch.setattr(pl, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(pl, "_CACHE_FILE", tmp_path / "pricing-cache.json")

    lookup = PriceLookup(aws_region="us-east-1", bq_location="us", use_cache=True)
    monkeypatch.setattr(lookup, "_fetch_aws_rates", lambda: pl.AWSRates(
        rpu_hour_usd=0.375, fetched_at="2026-07-04", source="AWS Price List API"))
    monkeypatch.setattr(lookup, "_fetch_gcp_rates", lambda client: pl.GCPRates(
        ondemand_usd_per_tib=6.25, fetched_at="2026-07-04",
        source="GCP Cloud Billing Catalog API (us)"))
    lookup.fetch(gcp_client=object())
    assert (tmp_path / "pricing-cache.json").exists()


def test_fetch_live_rates_times_out_before_slow_fetch_completes() -> None:
    """A fetch that outlasts the budget raises PricingTimeout at the deadline, not later.

    Regression for the storm-aws deep-audit finding that the paginated GCP catalog fetch
    could freeze the CLI for minutes. The wait ends at the budget, and the orphaned worker
    thread must not delay the return.
    """
    class SlowLookup(PriceLookup):
        def fetch(self, gcp_client=None):
            time.sleep(5)
            return PricingRates(is_live=True)

    t0 = time.monotonic()
    with pytest.raises(PricingTimeout):
        fetch_live_rates_with_timeout(SlowLookup(), budget_seconds=0.3)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"waited {elapsed:.2f}s — should abort near the 0.3s budget"


def test_fetch_live_rates_returns_result_within_budget() -> None:
    """A fast fetch returns its PricingRates unchanged, well inside the budget."""
    class FastLookup(PriceLookup):
        def fetch(self, gcp_client=None):
            return PricingRates(is_live=True, aws_region="ap-southeast-2", bq_location="australia-southeast1")

    rates = fetch_live_rates_with_timeout(FastLookup(), budget_seconds=5)
    assert rates.is_live
    assert rates.aws_region == "ap-southeast-2"


def test_fetch_live_rates_passes_gcp_client_through() -> None:
    """The gcp_client argument is forwarded to the wrapped fetch()."""
    seen = {}

    class RecordingLookup(PriceLookup):
        def fetch(self, gcp_client=None):
            seen["client"] = gcp_client
            return PricingRates(is_live=False)

    sentinel = object()
    fetch_live_rates_with_timeout(RecordingLookup(), gcp_client=sentinel, budget_seconds=5)
    assert seen["client"] is sentinel


@pytest.fixture
def _restore_aws_constants():
    """Snapshot/restore EVERYTHING apply_aws_region + apply_live_rates mutate —
    a partial restore leaks another region's rates into later tests."""
    import copy

    from bq_assess.engine.redshift import cost_constants as k

    snap = {n: getattr(k, n) for n in (
        "V1_RPU_HOUR_USD", "V1_SERVERLESS_1YR_ALL_UPFRONT_RPU_HOUR_USD",
        "V1_SERVERLESS_1YR_NO_UPFRONT_RPU_HOUR_USD", "V1_SERVERLESS_3YR_RPU_HOUR_USD",
        "V2_S3_TABLES_USD_PER_GB_MONTH_TIER1", "V2_S3_TABLES_USD_PER_GB_MONTH_TIER2",
        "V2_S3_TABLES_USD_PER_GB_MONTH_TIER3", "V2_INT_IA_USD_PER_GB_MONTH",
        "V2_INT_AIA_USD_PER_GB_MONTH", "V6_MANAGED_STORAGE_USD_PER_GB_MONTH",
        "V8_DATA_TRANSFER_OUT_USD_PER_GB",
        "AWS_PRICING_REGION", "AWS_REGION_SCOPE", "AWS_REGION_MATCH_NOTE",
        "AWS_REGION_MATCH_LABEL", "AWS_CONFIRMED_DATE", "AWS_PROVISIONED_CONFIRMED_DATE",
    )}
    nodes = (copy.deepcopy(k.V7_RG_NODE_TYPES), copy.deepcopy(k.V6_RA3_NODE_TYPES))
    yield
    for n, val in snap.items():
        setattr(k, n, val)
    k.V7_RG_NODE_TYPES.update(nodes[0])
    k.V6_RA3_NODE_TYPES.update(nodes[1])


def test_apply_live_rates_region_gate_rejects_mismatched_aws_half(
    _restore_aws_constants,
) -> None:
    """A live AWS snapshot fetched for a DIFFERENT region than the cascade installed
    must not apply — e.g. a bundle collected while asia-southeast2 mapped to Sydney
    replaying Sydney rates onto a report now priced in Jakarta (2026-08-13)."""
    import bq_assess.core.price_lookup as pl
    from bq_assess.engine.redshift import cost_constants as k

    assert k.apply_aws_region("ap-southeast-3")
    rpu_before = k.V1_RPU_HOUR_USD
    stale = pl.PricingRates(
        aws=pl.AWSRates(rpu_hour_usd=0.419, fetched_at="2026-08-10",
                        source="AWS Price List API"),
        is_live=True, aws_region="ap-southeast-2",
    )
    aws_applied, _ = pl.apply_live_rates(stale)
    assert aws_applied is False
    assert k.V1_RPU_HOUR_USD == rpu_before          # Jakarta rate survived
    assert k.AWS_PRICING_REGION == "ap-southeast-3"  # tag not re-stamped


def test_apply_live_rates_region_gate_accepts_matching_aws_half(
    _restore_aws_constants,
) -> None:
    """Same-region live snapshot still applies (the gate only rejects mismatches)."""
    import bq_assess.core.price_lookup as pl
    from bq_assess.engine.redshift import cost_constants as k

    assert k.apply_aws_region("ap-southeast-3")
    fresh = pl.PricingRates(
        aws=pl.AWSRates(rpu_hour_usd=0.4567, fetched_at="2026-08-13",
                        source="AWS Price List API"),
        is_live=True, aws_region="ap-southeast-3",
    )
    aws_applied, _ = pl.apply_live_rates(fresh)
    assert aws_applied is True
    assert k.V1_RPU_HOUR_USD == 0.4567
