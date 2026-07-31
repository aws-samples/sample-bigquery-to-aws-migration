"""Verified AWS lakehouse pricing constants — V1 (Serverless RPU), V2 (S3 Tables) + the
V3 slot→RPU bridge ASSUMPTION (R18.7).

Confirmed 2026-06-15 against the **AWS Price List API** (machine-readable, authoritative) with a
Wayback cross-read for the RPU-hour rate. Mirrors ``core/pricing_constants.py``: dated, sourced,
overridable via module-level assignment — never hardcode a guess elsewhere.

⚠️ The module-level constants default to US East (N. Virginia) / us-east-1, USD, on-demand.
Other regions differ and are generally HIGHER. Call ``apply_aws_region(region)`` — normally
with ``bq_location_to_aws_region(detected_bq_location)`` — to re-point the constants at that
region's verified rates (AWS_REGIONAL_RATES below); the CLI/CostEstimator do this
automatically so the AWS comparison is priced in the same geography as the BigQuery side.
A live Price List API lookup (``core/price_lookup.py``, regional URL) can then override.

References:
- V1 Redshift Serverless: https://aws.amazon.com/redshift/pricing/
  (Price List API SKU USE1-Redshift:ServerlessUsage = $0.375/RPU-Hr)
- V2 S3 Tables:           https://aws.amazon.com/s3/pricing/  (S3 Tables tab; Price List API AmazonS3 us-east-1)
"""

from __future__ import annotations

AWS_CONFIRMED_DATE: str = "2026-06-24"
AWS_REGION_SCOPE: str = "US East (N. Virginia) / us-east-1"
# The AWS region the module-level constants currently reflect (set by apply_aws_region).
AWS_PRICING_REGION: str = "us-east-1"
V1_SOURCE_URL: str = "https://aws.amazon.com/redshift/pricing/"
V2_SOURCE_URL: str = "https://aws.amazon.com/s3/pricing/"

# =============================================================================
# V1 — Redshift Serverless compute, $/RPU-hour (HIGH confidence; triple-confirmed)
# =============================================================================

V1_RPU_HOUR_USD: float = 0.375
V1_RPU_GB_MEMORY: int = 16          # 1 RPU = 16 GB memory (documentary only; NOT used to size by storage)
HOURS_PER_MONTH: float = 730.0      # matches pricing_constants' hourly×730 convention

# Serverless Reservations — commitment-based discounts (launched Apr 2025 / Feb 2026).
# Unlike on-demand (pay-per-second when active), reservations bill 24/7 for the committed RPUs.
# Source: https://docs.aws.amazon.com/redshift/latest/mgmt/serverless-billing-reserved.html
# 1-year terms (launched Apr 2025): two payment options.
V1_SERVERLESS_RESERVATION_1YR_ALL_UPFRONT_DISCOUNT: float = 0.24   # All Upfront, 1-year
V1_SERVERLESS_RESERVATION_1YR_NO_UPFRONT_DISCOUNT: float = 0.20    # No Upfront, 1-year
V1_SERVERLESS_1YR_ALL_UPFRONT_RPU_HOUR_USD: float = round(V1_RPU_HOUR_USD * (1 - 0.24), 4)
V1_SERVERLESS_1YR_NO_UPFRONT_RPU_HOUR_USD: float = round(V1_RPU_HOUR_USD * (1 - 0.20), 4)
# 3-year term (launched Feb 2026, GA): ~45% savings vs on-demand.
# Source: aws.amazon.com/about-aws/whats-new/2026/02/amazon-redshift-serverless-three-year-reservations
# Confirmed by internal APJ Analytics What's New deck (May 2026, slide 15).
V1_SERVERLESS_RESERVATION_3YR_DISCOUNT: float = 0.45
V1_SERVERLESS_3YR_RPU_HOUR_USD: float = round(V1_RPU_HOUR_USD * (1 - 0.45), 4)
AWS_SERVERLESS_RESERVATIONS_CONFIRMED_DATE: str = "2026-07-15"

# Break-even utilization for reservations: the active-hour fraction at which the 24/7
# reservation cost equals on-demand cost (reserved_rate / ondemand_rate).
# At utilization ABOVE this threshold, the reservation saves money vs on-demand.
V1_SERVERLESS_1YR_ALL_UPFRONT_BREAKEVEN_UTIL: float = round(1 - V1_SERVERLESS_RESERVATION_1YR_ALL_UPFRONT_DISCOUNT, 4)
V1_SERVERLESS_1YR_NO_UPFRONT_BREAKEVEN_UTIL: float = round(1 - V1_SERVERLESS_RESERVATION_1YR_NO_UPFRONT_DISCOUNT, 4)
V1_SERVERLESS_3YR_BREAKEVEN_UTIL: float = round(1 - V1_SERVERLESS_RESERVATION_3YR_DISCOUNT, 4)

# Overflow burst fraction: fraction of active hours during which peak usage exceeds
# committed RPUs. Models that overflow RPUs are transient bursts, not sustained for the
# full active period. Derived from typical query burst patterns (peak lasts ~30% of active time).
V1_OVERFLOW_BURST_FRACTION: float = 0.30

# Serverless base/step facts (HIGH; AWS mgmt docs). Used ONLY for the R18.4 no-data range floor.
SERVERLESS_MIN_RPU_FLOOR: int = 4       # minimum base capacity for Redshift Serverless
SERVERLESS_DEFAULT_BASE_RPU: int = 32   # moderate workload anchor (range high)
# Regional RPU cap (verified 2026-07-15). 1024 only in us-east-1/2, us-west-2, eu-west-1,
# eu-central-1; all other regions cap at 512.
SERVERLESS_MAX_RPU_BY_REGION: dict[str, int] = {
    "us-east-1": 1024, "us-east-2": 1024, "us-west-2": 1024,
    "eu-west-1": 1024, "eu-central-1": 1024,
}
SERVERLESS_DEFAULT_MAX_RPU: int = 512
# Hours/month the no-data range assumes the warehouse is "on". Models a typical business-day
# usage pattern: 8 hours/day × 22 working days ≈ 176 hours/month.
RANGE_ACTIVE_HOURS_PER_MONTH: float = 176.0

# =============================================================================
# V2 — S3 Tables Standard storage, $/GB-month, us-east-1 (HIGH confidence)
# ⚠️ S3 TABLES, not plain S3 Standard ($0.023) — the managed Iceberg/compaction layer is ~15%
#    dearer and adds the monitoring/compaction lines below that plain S3 lacks.
# Marginal tiering: store tier WIDTHS, not cumulative thresholds (the audit caught a 50 TB bug).
# Billed in GB (decimal, 10^9), per AWS convention — distinct from BigQuery's binary GiB.
# =============================================================================

V2_S3_TABLES_USD_PER_GB_MONTH_TIER1: float = 0.0265   # first 50 TB
V2_S3_TABLES_USD_PER_GB_MONTH_TIER2: float = 0.0253   # next 450 TB (50–500 TB)
V2_S3_TABLES_USD_PER_GB_MONTH_TIER3: float = 0.0242   # over 500 TB
V2_TIER1_WIDTH_GB: float = 50.0 * 1000      # first 50 TB, in decimal GB
V2_TIER2_WIDTH_GB: float = 450.0 * 1000     # next 450 TB (NOT 500 — marginal width)
GB_PER_BYTE: float = 1e-9                    # AWS storage billing: bytes → decimal GB

# Physical-bytes fallback ratio: canonical value lives in core/storage_stats.py
# (collection-time concern — the collector distribution must not import the engine).
# Re-exported so cost/report call sites keep their k.ASSUMED_PHYSICAL_RATIO idiom.
from bq_assess.core.storage_stats import ASSUMED_PHYSICAL_RATIO  # noqa: F401

# S3-Tables-only recurring maintenance lines (plain S3 lacks these) — the "negligible
# request/maintenance lines" R18.1 folds alongside storage. Object monitoring is the
# recurring one and IS billed into the storage line (object count estimated from
# physical bytes at the post-compaction target object size). Compaction bills per GB
# of NEWLY WRITTEN data — unknowable without an ingestion rate, so it stays a
# pricing-note disclosure. Request charges remain out of scope (scope note).
V2_OBJECT_MONITORING_USD_PER_1K_OBJECTS_MONTH: float = 0.025
V2_COMPACTION_USD_PER_1K_OBJECTS: float = 0.002
V2_COMPACTION_USD_PER_GB_PROCESSED: float = 0.005
V2_REQUEST_TIER1_USD_PER_1K: float = 0.005      # PUT/COPY/POST/LIST
V2_REQUEST_TIER2_USD_PER_1K: float = 0.0004     # GET/other
# Assumed average object size AFTER compaction (S3 Tables' default target file size
# is 512 MB; real estates land below target on small partitions — 128 MB is the
# conservative-high object-count anchor for the monitoring estimate).
V2_ASSUMED_OBJECT_SIZE_MB: float = 128.0

# =============================================================================
# V2 — S3 Tables Intelligent-Tiering storage class, $/GB-month, us-east-1
# (verified 2026-07-31 against the AmazonS3 Price List offer file)
# The INT storage class tiers FILES within a table by access recency:
#   Frequent Access — default; bills at EXACTLY the Standard tier rates above
#     (confirmed SKU-by-SKU: Tables-TimedStorage-INT-FA-ByteHrs carries the same
#     three volume-tier prices as Tables-TimedStorage-ByteHrs) → no FA constants.
#   Infrequent Access — after 30 days unaccessed; FLAT rate, no volume tiers.
#   Archive Instant Access — after 90 days unaccessed; FLAT rate, no volume tiers.
# All three serve reads at millisecond latency; a read moves the file back to
# Frequent at no retrieval charge. Monitoring bills through the SAME
# Tables-MonitoredObjects SKU already priced above — no separate INT fee.
# Live values overwrite these via apply_live_rates (same path as the Standard
# tiers); the fallback constants are us-east-1.
# ⚠️ The cost model's tier split uses last_modified as an ACCESS proxy (we do
# not collect per-table read recency) — every surface showing the tiered figure
# must carry that caveat.
# =============================================================================

V2_INT_INFREQUENT_THRESHOLD_DAYS: int = 30
V2_INT_ARCHIVE_THRESHOLD_DAYS: int = 90
V2_INT_IA_USD_PER_GB_MONTH: float = 0.0144    # Tables-TimedStorage-INT-IA-ByteHrs
V2_INT_AIA_USD_PER_GB_MONTH: float = 0.0046   # Tables-TimedStorage-INT-AIA-ByteHrs

# =============================================================================
# V3 — slot→RPU bridge.  ⚠️⚠️ LOW-CONFIDENCE ASSUMPTION, NOT A VERIFIED FACT. ⚠️⚠️
# There is NO published AWS/GCP slot↔RPU equivalence. Cross-referencing hardware specs
# (1 RPU = 2 vCPU + 16 GB; 1 BQ slot ≈ 0.5 vCPU) yields ~0.25; the Fivetran 2022 benchmark
# (300 BQ slots ≈ 18 RPU-equiv at performance parity) yields ~0.06; pure cost ratio ($0.06 vs
# $0.375 per unit-hr) yields ~0.16. We use 0.15 — the midpoint of the 0.06–0.25 evidence
# range (lowered from 0.20 on 2026-07-23; history: 0.15→0.20 on 2026-07-05 after the Montu
# reconciliation, back to 0.15 with the serverless 4-RPU active-hours billing floor now
# catching the understatement risk on always-on workloads). MUST be replaced by empirical
# RPU-hour measurement (SYS_SERVERLESS_USAGE) on a representative migrated workload before
# quoting. Every emitted compute line carries a visible ASSUMPTION label (R18.7).
# =============================================================================

V3_SLOT_TO_RPU_RATIO: float = 0.15
V3_CONFIDENCE_IS_ASSUMPTION: bool = True
V3_ASSUMPTION_NOTE: str = (
    "slot→RPU 0.15 ASSUMPTION (evidence range: 0.06–0.25, set at midpoint; "
    "no published equivalence; overridable; verify with empirical RPU-hour "
    "measurement before quoting)"
)

# =============================================================================
# Plan vocabulary bridge — PricingDetection.commitment_plan → V4_EDITION_SLOT_HOUR_USD sub-key.
# PricingDetection emits {FLEX, MONTHLY, ANNUAL, THREE_YEAR}; the V4 rate table keys on
# {payg, commit_1yr, commit_3yr}. FLEX/MONTHLY have no slot commitment → payg.
# =============================================================================

COMMITMENT_PLAN_TO_RATE_KEY: dict[str, str] = {
    "FLEX": "payg",
    "MONTHLY": "payg",
    "ANNUAL": "commit_1yr",
    "THREE_YEAR": "commit_3yr",
}

# =============================================================================
# Migration one-time cost — derived from aggregate Migration Effort (R9), NOT a per-table fee
# (R18.5). $/effort-point; calibration-tunable (R9.2 weights are "subject to calibration").
# =============================================================================

MIGRATION_USD_PER_EFFORT_POINT: float = 5.0

# =============================================================================
# BigQuery on-demand scan proxy — when no query logs/slots are available, estimate monthly
# bytes scanned as a fraction of stored bytes per day (R18.2a). Labelled LOW-confidence estimate.
# =============================================================================

BQ_DAILY_SCAN_FRACTION: float = 0.10    # 10% of stored bytes scanned/day (legacy proxy, retained)
DAYS_PER_MONTH: float = 30.0

# Break-even sentinel: finite (JSON-safe) stand-in for "migration never recoups". 9999 months
# (~833 years) is semantically equivalent to "never" for any business decision.
BREAKEVEN_NEVER: float = 9999.0

# =============================================================================
# V6 — Redshift Provisioned RA3, $/node-hour, us-east-1 (HIGH confidence)
# Confirmed 2026-06-15 against the AWS Price List API.
# References: https://aws.amazon.com/redshift/pricing/
# =============================================================================

AWS_PROVISIONED_CONFIRMED_DATE: str = "2026-06-24"

# =============================================================================
# V7 — Redshift Graviton (RG) instances, $/node-hour, us-east-1 (HIGH confidence)
# GA May 12, 2026. Graviton4-powered, 30% lower price/vCPU vs RA3, eliminates
# separate Spectrum per-TB charges (built-in data lake query engine).
# Source: https://aws.amazon.com/redshift/pricing/
# Migration from RA3: 4:3 node mapping (4× ra3.4xl → 3× rg.4xl) for 25% infra savings.
# =============================================================================

V7_RG_NODE_TYPES: dict[str, dict] = {
    "rg.xlarge": {
        "vcpu": 4,
        "memory_gb": 32,
        "ondemand_usd_per_node_hour": 0.76,
        "ri_1yr_usd_per_node_hour": 0.532,
        "ri_3yr_usd_per_node_hour": 0.331,
        "min_nodes": 2,
        "max_nodes": 32,
    },
    "rg.4xlarge": {
        "vcpu": 16,
        "memory_gb": 128,
        "ondemand_usd_per_node_hour": 3.043,
        "ri_1yr_usd_per_node_hour": 2.130,
        "ri_3yr_usd_per_node_hour": 1.324,
        "min_nodes": 2,
        "max_nodes": 32,  # conservative; AWS docs confirm 128 for ra3 but RG limit unverified
    },
}

# Legacy RA3 types retained for reference/fallback (regions without RG availability).
V6_RA3_NODE_TYPES: dict[str, dict] = {
    "ra3.xlplus": {
        "vcpu": 4,
        "memory_gb": 32,
        "ondemand_usd_per_node_hour": 1.086,
        "ri_1yr_usd_per_node_hour": 0.760,
        "ri_3yr_usd_per_node_hour": 0.473,
        "min_nodes": 2,
        "max_nodes": 32,
    },
    "ra3.4xlarge": {
        "vcpu": 12,
        "memory_gb": 96,
        "ondemand_usd_per_node_hour": 3.26,
        "ri_1yr_usd_per_node_hour": 2.282,
        "ri_3yr_usd_per_node_hour": 1.418,
        "min_nodes": 2,
        "max_nodes": 32,
    },
    "ra3.16xlarge": {
        "vcpu": 48,
        "memory_gb": 384,
        "ondemand_usd_per_node_hour": 13.04,
        "ri_1yr_usd_per_node_hour": 9.128,
        "ri_3yr_usd_per_node_hour": 5.672,
        "min_nodes": 2,
        "max_nodes": 128,
    },
}

# Redshift Managed Storage (RMS): applies to all RA3 node types.
V6_MANAGED_STORAGE_USD_PER_GB_MONTH: float = 0.024

# Concurrency scaling: 1 free hour/day/cluster; billed at same node-hour rate beyond that.
V6_CONCURRENCY_SCALING_FREE_HOURS_PER_DAY: float = 1.0
# Fraction of base cost to add for concurrency scaling burst (workload-dependent estimate).
V6_CONCURRENCY_SCALING_OVERHEAD_FRACTION: float = 0.20

# =============================================================================
# Region cascade — BigQuery dataset location → nearest AWS region, plus verified
# per-region AWS rates. Verified 2026-07-02 against the AWS Price List API
# (AmazonRedshift + AmazonS3 regional offer files). The comparison must price both
# clouds in the same geography: an australia-southeast1 Source gets its Query Engine
# (Redshift Serverless / provisioned nodes) and storage priced in ap-southeast-2,
# not us-east-1.
# =============================================================================

# BigQuery location token (lowercase) → AWS region: canonical mapping lives in
# core/region_mapping.py (the collector distribution records aws_region in the bundle
# manifest without importing the engine). Re-exported here for existing call sites.
from bq_assess.core.region_mapping import (  # noqa: F401
    BQ_LOCATION_TO_AWS_REGION,
    bq_location_to_aws_region,
)

# Per-AWS-region rates (Price List API, 2026-07-02). Only rg.xlarge and rg.4xlarge exist
# (verified 2026-07-15; rg.16xlarge was removed — the node type does not exist).
AWS_REGIONAL_RATES: dict[str, dict] = {
    "us-east-1": {
        "label": "US East (N. Virginia)",
        "rpu_hour": 0.375, "rms": 0.024,
        "s3_tables": (0.0265, 0.0253, 0.0242),
        "s3_int": (0.0144, 0.0046),
        "rg.xlarge": (0.76, 0.532, 0.331), "rg.4xlarge": (3.043, 2.130, 1.324),
        "ra3.xlplus": (1.086, 0.760, 0.473), "ra3.4xlarge": (3.26, 2.282, 1.418), "ra3.16xlarge": (13.04, 9.128, 5.672),
    },
    "us-west-2": {
        "label": "US West (Oregon)",
        "rpu_hour": 0.36, "rms": 0.024,
        "s3_tables": (0.0265, 0.0253, 0.0242),
        "s3_int": (0.0144, 0.0046),
        "rg.xlarge": (0.7602, 0.53214, 0.33075), "rg.4xlarge": (3.04267, 2.12987, 1.32356),
        "ra3.xlplus": (1.086, 0.7602, 0.4725), "ra3.4xlarge": (3.26, 2.282, 1.4181), "ra3.16xlarge": (13.04, 9.128, 5.6724),
    },
    "ca-central-1": {
        "label": "Canada (Central)",
        "rpu_hour": 0.4125, "rms": 0.0261,
        "s3_tables": (0.0288, 0.0276, 0.0265),
        "s3_int": (0.0159, 0.0058),
        "rg.xlarge": (0.8414, 0.58898, 0.36603), "rg.4xlarge": (3.36653, 2.35723, 1.46487),
        "ra3.xlplus": (1.202, 0.8414, 0.5229), "ra3.4xlarge": (3.607, 2.5256, 1.5695), "ra3.16xlarge": (14.43, 10.101, 6.2771),
    },
    "sa-east-1": {
        "label": "South America (São Paulo)",
        "rpu_hour": 0.5976, "rms": 0.043,
        "s3_tables": (0.0466, 0.0449, 0.0426),
        "s3_int": (0.0254, 0.0095),
        "rg.xlarge": (1.2117, 0.8484, 0.5271), "rg.4xlarge": (4.84867, 3.39407, 2.10924),
        "ra3.xlplus": (1.731, 1.212, 0.753), "ra3.4xlarge": (5.195, 3.6365, 2.2599), "ra3.16xlarge": (20.78, 14.546, 9.0393),
    },
    "eu-west-1": {
        "label": "Europe (Ireland)",
        "rpu_hour": 0.387, "rms": 0.024,
        "s3_tables": (0.0265, 0.0253, 0.0242),
        "s3_int": (0.0144, 0.0046),
        "rg.xlarge": (0.8414, 0.58898, 0.36603), "rg.4xlarge": (3.3656, 2.35592, 1.46412),
        "ra3.xlplus": (1.202, 0.8414, 0.5229), "ra3.4xlarge": (3.606, 2.5242, 1.5687), "ra3.16xlarge": (14.424, 10.0968, 6.2745),
    },
    "eu-west-2": {
        "label": "Europe (London)",
        "rpu_hour": 0.467, "rms": 0.025,
        "s3_tables": (0.0276, 0.0265, 0.0253),
        "s3_int": (0.0151, 0.0058),
        "rg.xlarge": (0.8848, 0.61936, 0.38493), "rg.4xlarge": (3.54013, 2.47809, 1.54),
        "ra3.xlplus": (1.264, 0.8848, 0.5499), "ra3.4xlarge": (3.793, 2.6551, 1.65), "ra3.16xlarge": (15.174, 10.6218, 6.6007),
    },
    "eu-central-1": {
        "label": "Europe (Frankfurt)",
        "rpu_hour": 0.451, "rms": 0.0256,
        "s3_tables": (0.0282, 0.027, 0.0259),
        "s3_int": (0.0155, 0.0058),
        "rg.xlarge": (0.9086, 0.63602, 0.39529), "rg.4xlarge": (3.6344, 2.54408, 1.58097),
        "ra3.xlplus": (1.298, 0.9086, 0.5647), "ra3.4xlarge": (3.894, 2.7258, 1.6939), "ra3.16xlarge": (15.578, 10.9046, 6.7765),
    },
    "ap-southeast-2": {
        "label": "Asia Pacific (Sydney)",
        "rpu_hour": 0.419, "rms": 0.0261,
        "s3_tables": (0.0288, 0.0276, 0.0265),
        "s3_int": (0.0159, 0.0058),
        "rg.xlarge": (0.9121, 0.63847, 0.39683), "rg.4xlarge": (3.6484, 2.55388, 1.58713),
        "ra3.xlplus": (1.303, 0.9121, 0.5669), "ra3.4xlarge": (3.909, 2.7363, 1.7005), "ra3.16xlarge": (15.636, 10.9452, 6.8017),
    },
    "ap-southeast-1": {
        "label": "Asia Pacific (Singapore)",
        "rpu_hour": 0.45, "rms": 0.0261,
        "s3_tables": (0.0288, 0.0276, 0.0265),
        "s3_int": (0.0159, 0.0058),
        "rg.xlarge": (0.9121, 0.63847, 0.39683), "rg.4xlarge": (3.6484, 2.55388, 1.58713),
        "ra3.xlplus": (1.303, 0.9121, 0.5669), "ra3.4xlarge": (3.909, 2.7363, 1.7005), "ra3.16xlarge": (15.636, 10.9452, 6.8017),
    },
    "ap-northeast-1": {
        "label": "Asia Pacific (Tokyo)",
        "rpu_hour": 0.494, "rms": 0.0261,
        "s3_tables": (0.0288, 0.0276, 0.0265),
        "s3_int": (0.0159, 0.0058),
        "rg.xlarge": (0.8946, 0.62622, 0.3892), "rg.4xlarge": (3.58027, 2.50619, 1.55745),
        "ra3.xlplus": (1.278, 0.8946, 0.556), "ra3.4xlarge": (3.836, 2.6852, 1.6687), "ra3.16xlarge": (15.347, 10.7429, 6.676),
    },
    "ap-south-1": {
        "label": "Asia Pacific (Mumbai)",
        "rpu_hour": 0.4275, "rms": 0.0261,
        "s3_tables": (0.0288, 0.0276, 0.0265),
        "s3_int": (0.0159, 0.0058),
        "rg.xlarge": (0.8645, 0.60515, 0.37611), "rg.4xlarge": (3.45893, 2.42125, 1.50472),
        "ra3.xlplus": (1.235, 0.8645, 0.5373), "ra3.4xlarge": (3.706, 2.5942, 1.6122), "ra3.16xlarge": (14.827, 10.3789, 6.4498),
    },
}


def apply_aws_region(region: str) -> bool:
    """Re-point the module-level AWS rate constants at ``region``'s verified rates.

    Returns True when the region is in the verified table (constants updated), False when
    unknown (constants left at us-east-1 so callers can attach a caveat). Same
    overridable-module-assignment contract as apply_live_rates (R18.7).
    """
    global V1_RPU_HOUR_USD, V1_SERVERLESS_1YR_ALL_UPFRONT_RPU_HOUR_USD
    global V1_SERVERLESS_1YR_NO_UPFRONT_RPU_HOUR_USD, V1_SERVERLESS_3YR_RPU_HOUR_USD
    global V2_S3_TABLES_USD_PER_GB_MONTH_TIER1, V2_S3_TABLES_USD_PER_GB_MONTH_TIER2
    global V2_S3_TABLES_USD_PER_GB_MONTH_TIER3, V6_MANAGED_STORAGE_USD_PER_GB_MONTH
    global V2_INT_IA_USD_PER_GB_MONTH, V2_INT_AIA_USD_PER_GB_MONTH
    global AWS_PRICING_REGION, AWS_REGION_SCOPE

    rates = AWS_REGIONAL_RATES.get(region)
    if rates is None:
        return False

    V1_RPU_HOUR_USD = rates["rpu_hour"]
    V1_SERVERLESS_1YR_ALL_UPFRONT_RPU_HOUR_USD = round(
        V1_RPU_HOUR_USD * (1 - V1_SERVERLESS_RESERVATION_1YR_ALL_UPFRONT_DISCOUNT), 4
    )
    V1_SERVERLESS_1YR_NO_UPFRONT_RPU_HOUR_USD = round(
        V1_RPU_HOUR_USD * (1 - V1_SERVERLESS_RESERVATION_1YR_NO_UPFRONT_DISCOUNT), 4
    )
    V1_SERVERLESS_3YR_RPU_HOUR_USD = round(
        V1_RPU_HOUR_USD * (1 - V1_SERVERLESS_RESERVATION_3YR_DISCOUNT), 4
    )
    t1, t2, t3 = rates["s3_tables"]
    V2_S3_TABLES_USD_PER_GB_MONTH_TIER1 = t1
    V2_S3_TABLES_USD_PER_GB_MONTH_TIER2 = t2
    V2_S3_TABLES_USD_PER_GB_MONTH_TIER3 = t3
    V6_MANAGED_STORAGE_USD_PER_GB_MONTH = rates["rms"]
    ia, aia = rates["s3_int"]
    V2_INT_IA_USD_PER_GB_MONTH = ia
    V2_INT_AIA_USD_PER_GB_MONTH = aia

    for node_type, table in (("rg.xlarge", V7_RG_NODE_TYPES), ("rg.4xlarge", V7_RG_NODE_TYPES),
                             ("ra3.xlplus", V6_RA3_NODE_TYPES), ("ra3.4xlarge", V6_RA3_NODE_TYPES),
                             ("ra3.16xlarge", V6_RA3_NODE_TYPES)):
        od, ri1, ri3 = rates[node_type]
        table[node_type]["ondemand_usd_per_node_hour"] = od
        table[node_type]["ri_1yr_usd_per_node_hour"] = ri1
        table[node_type]["ri_3yr_usd_per_node_hour"] = ri3

    AWS_PRICING_REGION = region
    AWS_REGION_SCOPE = f"{rates['label']} / {region}"
    return True


# =============================================================================
# V6 — Cluster sizing heuristics (from workload metrics)
# =============================================================================

# Queries/day threshold: workloads above this use rg.4xlarge instead of rg.xlarge.
V6_QUERIES_PER_DAY_XLPLUS_MAX: int = 50_000

# vCPUs needed per concurrent query slot (heuristic; BQ on-demand typically runs
# queries with 1-4 effective slot equivalents for small scans, more for large scans).
V6_VCPU_PER_CONCURRENT_QUERY: float = 1.5

# Assumed average query duration in seconds (for concurrency estimation from QPD).
V6_AVG_QUERY_DURATION_SECONDS: float = 7.0
# Peak-to-average ratio for query concurrency (business-hour burst factor).
V6_PEAK_TO_AVG_CONCURRENCY_RATIO: float = 3.0
