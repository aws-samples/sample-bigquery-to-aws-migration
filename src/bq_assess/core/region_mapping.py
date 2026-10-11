"""BQ location → AWS region mapping (shared by collector and full tool).

Policy: price AWS at the geographically closest region that offers the full
target stack (S3 Tables + Redshift Serverless + rg nodes). The mapping is
computed from city coordinates at import time — nobody hand-edits pairings
(the old hand-picked table shipped Osaka→Seoul and Seoul→Osaka for months).

Three match kinds, surfaced to the report so a non-obvious choice is visible:
- "nearest":  pure haversine winner over AWS_REGIONS_WITH_STACK. Carries a
              note when the winner is far away (Middle East locations route
              to Mumbai/Milan because il-central-1, me-central-1 and
              me-south-1 lack rg nodes and/or S3 Tables).
- "pinned":   judgment call where raw distance is misleading (multi-regions
              have no city; cross-border winners lose to a same-country
              region that is barely further). Reasons live in PINNED_BQ_LOCATIONS.
- "fallback": unknown BQ location → us-east-1; the report must flag it.

Coordinates are metro-center approximations — precision is irrelevant at
region scale. Maintenance: add a row when GCP or AWS launches a region, and
refresh AWS_REGIONS_WITH_STACK against the Price List API (see the module
docstring date stamps).
"""

from __future__ import annotations

import math
from typing import NamedTuple

# BQ location → (city, lat, lon). Source: GCP locations page (2026-08-13).
BQ_LOCATION_COORDS: dict[str, tuple[str, float, float]] = {
    "us-central1": ("Council Bluffs, Iowa", 41.26, -95.86),
    "us-east1": ("Moncks Corner, South Carolina", 33.20, -80.01),
    "us-east4": ("Ashburn, Virginia", 39.04, -77.49),
    "us-east5": ("Columbus, Ohio", 39.96, -83.00),
    "us-south1": ("Dallas, Texas", 32.78, -96.80),
    "us-west1": ("The Dalles, Oregon", 45.59, -121.18),
    "us-west2": ("Los Angeles", 34.05, -118.24),
    "us-west3": ("Salt Lake City", 40.76, -111.89),
    "us-west4": ("Las Vegas", 36.17, -115.14),
    "northamerica-northeast1": ("Montréal", 45.50, -73.57),
    "northamerica-northeast2": ("Toronto", 43.65, -79.38),
    "northamerica-south1": ("Querétaro, Mexico", 20.59, -100.39),
    "southamerica-east1": ("São Paulo", -23.55, -46.63),
    "southamerica-west1": ("Santiago", -33.45, -70.67),
    "europe-west1": ("St. Ghislain, Belgium", 50.45, 3.82),
    "europe-west2": ("London", 51.51, -0.13),
    "europe-west3": ("Frankfurt", 50.11, 8.68),
    "europe-west4": ("Eemshaven, Netherlands", 53.44, 6.83),
    "europe-west6": ("Zurich", 47.38, 8.54),
    "europe-west8": ("Milan", 45.46, 9.19),
    "europe-west9": ("Paris", 48.86, 2.35),
    "europe-west10": ("Berlin", 52.52, 13.40),
    "europe-west12": ("Turin", 45.07, 7.69),
    "europe-central2": ("Warsaw", 52.23, 21.01),
    "europe-north1": ("Hamina, Finland", 60.57, 27.20),
    "europe-north2": ("Stockholm", 59.33, 18.06),
    "europe-southwest1": ("Madrid", 40.42, -3.70),
    "asia-east1": ("Changhua, Taiwan", 24.05, 120.52),
    "asia-east2": ("Hong Kong", 22.32, 114.17),
    "asia-northeast1": ("Tokyo", 35.68, 139.69),
    "asia-northeast2": ("Osaka", 34.69, 135.50),
    "asia-northeast3": ("Seoul", 37.57, 126.98),
    "asia-south1": ("Mumbai", 19.08, 72.88),
    "asia-south2": ("Delhi", 28.61, 77.21),
    "asia-southeast1": ("Singapore", 1.35, 103.82),
    "asia-southeast2": ("Jakarta", -6.21, 106.85),
    "australia-southeast1": ("Sydney", -33.87, 151.21),
    "australia-southeast2": ("Melbourne", -37.81, 144.96),
    "me-central1": ("Doha", 25.29, 51.53),
    "me-central2": ("Dammam, Saudi Arabia", 26.43, 50.10),
    "me-west1": ("Tel Aviv", 32.08, 34.78),
    "africa-south1": ("Johannesburg", -26.20, 28.05),
}

# AWS region → (city, lat, lon). Commercial partition (2026-08-13).
AWS_REGION_COORDS: dict[str, tuple[str, float, float]] = {
    "us-east-1": ("N. Virginia (Ashburn)", 39.04, -77.49),
    "us-east-2": ("Ohio (Columbus)", 39.96, -83.00),
    "us-west-1": ("N. California", 37.77, -122.42),
    "us-west-2": ("Oregon (Boardman)", 45.84, -119.70),
    "ca-central-1": ("Montréal", 45.50, -73.57),
    "ca-west-1": ("Calgary", 51.05, -114.07),
    "mx-central-1": ("Querétaro", 20.59, -100.39),
    "sa-east-1": ("São Paulo", -23.55, -46.63),
    "eu-west-1": ("Dublin", 53.35, -6.26),
    "eu-west-2": ("London", 51.51, -0.13),
    "eu-west-3": ("Paris", 48.86, 2.35),
    "eu-central-1": ("Frankfurt", 50.11, 8.68),
    "eu-central-2": ("Zurich", 47.38, 8.54),
    "eu-south-1": ("Milan", 45.46, 9.19),
    "eu-south-2": ("Spain (Aragón)", 41.65, -0.88),
    "eu-north-1": ("Stockholm", 59.33, 18.06),
    "il-central-1": ("Tel Aviv", 32.08, 34.78),
    "me-south-1": ("Bahrain", 26.23, 50.59),
    "me-central-1": ("UAE (Dubai)", 25.20, 55.27),
    "af-south-1": ("Cape Town", -33.92, 18.42),
    "ap-east-1": ("Hong Kong", 22.32, 114.17),
    "ap-east-2": ("Taipei", 25.03, 121.57),
    "ap-northeast-1": ("Tokyo", 35.68, 139.69),
    "ap-northeast-2": ("Seoul", 37.57, 126.98),
    "ap-northeast-3": ("Osaka", 34.69, 135.50),
    "ap-south-1": ("Mumbai", 19.08, 72.88),
    "ap-south-2": ("Hyderabad", 17.39, 78.49),
    "ap-southeast-1": ("Singapore", 1.35, 103.82),
    "ap-southeast-2": ("Sydney", -33.87, 151.21),
    "ap-southeast-3": ("Jakarta", -6.21, 106.85),
    "ap-southeast-4": ("Melbourne", -37.81, 144.96),
    "ap-southeast-5": ("Malaysia (Kuala Lumpur)", 3.14, 101.69),
    "ap-southeast-7": ("Thailand (Bangkok)", 13.76, 100.50),
}

# Regions offering the full target stack: S3 Tables + Redshift Serverless +
# rg (Redshift Gen) nodes. Verified 2026-08-13 against the AWS Price List API
# regional offer files (AmazonRedshift + AmazonS3). Notable absences: il-central-1,
# me-central-1, me-south-1, eu-central-2, ca-west-1 lack rg nodes and/or S3 Tables.
AWS_REGIONS_WITH_STACK: frozenset[str] = frozenset({
    "af-south-1", "ap-east-1", "ap-east-2",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-south-1", "ap-south-2",
    "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4",
    "ap-southeast-5", "ap-southeast-7",
    "ca-central-1", "eu-central-1", "eu-north-1",
    "eu-south-1", "eu-south-2",
    "eu-west-1", "eu-west-2", "eu-west-3",
    "mx-central-1", "sa-east-1",
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
})

# Judgment pins — where raw distance is misleading. (region, user-facing reason)
PINNED_BQ_LOCATIONS: dict[str, tuple[str, str]] = {
    "us": ("us-east-1", "US multi-region has no single location; priced in us-east-1"),
    "eu": ("eu-west-1", "EU multi-region has no single location; priced in eu-west-1"),
    "us-south1": (
        "us-east-2",
        "nearest same-country region (mx-central-1 is marginally closer but cross-border)",
    ),
    "northamerica-northeast2": (
        "ca-central-1",
        "nearest same-country region (us-east-2 is equidistant but cross-border)",
    ),
    "europe-central2": (
        "eu-central-1",
        (
            "eu-north-1 is marginally closer by distance; Frankfurt is the established "
            "low-latency path from Poland"
        ),
    ),
}

# A clean nearest match further than this gets a distance note in the report —
# it means no nearby region offers the full stack (Middle East, Santiago).
_DISTANT_MATCH_KM = 1500.0

_FALLBACK_REGION = "us-east-1"


# Multi-region pins are the unsurprising default (us→us-east-1, eu→eu-west-1) and
# the most common case — badging them would train readers to ignore the badge.
# Their note still reaches the methodology text; only the badge is suppressed.
_QUIET_PINS = frozenset({"us", "eu"})


class RegionMatch(NamedTuple):
    """Result of mapping a BQ location to an AWS pricing region."""

    aws_region: str
    kind: str  # "nearest" | "pinned" | "fallback"
    note: str  # empty for a clean nearest match; rendered in the methodology text
    distance_km: float | None
    ui_label: str = ""  # badge text ("pinned region" | "fallback region" | "distant region"); empty = no badge


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def nearest_aws_regions(location: str) -> list[tuple[float, str]]:
    """Full-stack AWS regions ranked by distance from ``location``, nearest first.

    Returns [] for locations without a coordinate entry (multi-regions, unknowns).
    """
    coords = BQ_LOCATION_COORDS.get((location or "").strip().lower())
    if coords is None:
        return []
    _, lat, lon = coords
    return sorted(
        (_haversine_km(lat, lon, alat, alon), region)
        for region, (_, alat, alon) in AWS_REGION_COORDS.items()
        if region in AWS_REGIONS_WITH_STACK
    )


def match_bq_location(location: str | None) -> RegionMatch:
    """Map a BQ location to its AWS pricing region, with provenance.

    Precedence: pin → nearest full-stack region by distance → us-east-1 fallback.
    """
    loc = (location or "").strip().lower()

    pinned = PINNED_BQ_LOCATIONS.get(loc)
    if pinned is not None:
        region, reason = pinned
        label = "" if loc in _QUIET_PINS else "pinned region"
        return RegionMatch(region, "pinned", reason, None, label)

    ranked = nearest_aws_regions(loc)
    if ranked:
        distance_km, region = ranked[0]
        note = ""
        label = ""
        if distance_km > _DISTANT_MATCH_KM:
            city = BQ_LOCATION_COORDS[loc][0]
            note = (
                f"nearest AWS region with S3 Tables + Redshift (rg) availability "
                f"is ~{distance_km:,.0f} km from {city}"
            )
            label = "distant region"
        return RegionMatch(region, "nearest", note, distance_km, label)

    return RegionMatch(
        _FALLBACK_REGION,
        "fallback",
        f"BigQuery location '{loc or 'unknown'}' has no coordinate entry — "
        f"defaulted to {_FALLBACK_REGION}; override with --target-region",
        None,
        "fallback region",
    )


# Snapshot of the computed policy, kept for existing call sites and grep-ability.
# Derived at import time — do not hand-edit pairings; fix coordinates or pins.
BQ_LOCATION_TO_AWS_REGION: dict[str, str] = {
    loc: match_bq_location(loc).aws_region for loc in BQ_LOCATION_COORDS
} | {loc: region for loc, (region, _) in PINNED_BQ_LOCATIONS.items()}


def bq_location_to_aws_region(location: str | None) -> tuple[str, bool]:
    """Back-compat wrapper over :func:`match_bq_location`.

    Returns:
        Tuple of (aws_region, is_fallback).
        is_fallback=True when the location is unknown and us-east-1 fallback is used.
    """
    match = match_bq_location(location)
    return (match.aws_region, match.kind == "fallback")
