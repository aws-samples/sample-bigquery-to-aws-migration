"""Tests for BigQuery location → AWS region mapping (nearest-region policy)."""
from __future__ import annotations

from bq_assess.core.region_mapping import (
    AWS_REGION_COORDS,
    AWS_REGIONS_WITH_STACK,
    BQ_LOCATION_COORDS,
    BQ_LOCATION_TO_AWS_REGION,
    PINNED_BQ_LOCATIONS,
    bq_location_to_aws_region,
    match_bq_location,
    nearest_aws_regions,
)

# ── Nearest-match pairings ─────────────────────────────────────────────


def test_same_city_pairs_map_exactly():
    """A BQ location with an AWS region in the same metro must map to it."""
    same_city = {
        "asia-southeast2": "ap-southeast-3",   # Jakarta — the flip case
        "asia-northeast1": "ap-northeast-1",   # Tokyo
        "asia-northeast2": "ap-northeast-3",   # Osaka (old map shipped Seoul)
        "asia-northeast3": "ap-northeast-2",   # Seoul (old map shipped Osaka)
        "asia-east2": "ap-east-1",             # Hong Kong
        "europe-west2": "eu-west-2",           # London
        "europe-west3": "eu-central-1",        # Frankfurt
        "europe-west8": "eu-south-1",          # Milan
        "europe-west9": "eu-west-3",           # Paris
        "europe-north2": "eu-north-1",         # Stockholm
        "northamerica-northeast1": "ca-central-1",  # Montréal
        "northamerica-south1": "mx-central-1",      # Querétaro
        "southamerica-east1": "sa-east-1",     # São Paulo
        "asia-south1": "ap-south-1",           # Mumbai
        "asia-southeast1": "ap-southeast-1",   # Singapore
        "australia-southeast1": "ap-southeast-2",  # Sydney
        "australia-southeast2": "ap-southeast-4",  # Melbourne
        "us-east4": "us-east-1",               # Ashburn
        "us-east5": "us-east-2",               # Columbus
    }
    for loc, expected in same_city.items():
        match = match_bq_location(loc)
        assert match.aws_region == expected, f"{loc}: {match.aws_region} != {expected}"
        assert match.kind == "nearest"
        assert match.note == ""      # same-metro matches never carry a flag
        assert match.ui_label == ""  # and never a badge


def test_nearest_by_distance():
    """Locations without a same-metro region go to the closest full-stack region."""
    assert match_bq_location("asia-east1").aws_region == "ap-east-2"      # Taiwan → Taipei
    assert match_bq_location("europe-west1").aws_region == "eu-west-3"    # Belgium → Paris
    assert match_bq_location("europe-west10").aws_region == "eu-central-1"  # Berlin → Frankfurt
    assert match_bq_location("europe-west12").aws_region == "eu-south-1"  # Turin → Milan
    assert match_bq_location("us-central1").aws_region == "us-east-2"     # Iowa → Ohio
    assert match_bq_location("us-west2").aws_region == "us-west-1"        # LA → N. California


def test_stack_gate_reroutes_when_nearest_region_lacks_services():
    """il-central-1 / me-*-1 / eu-central-2 lack rg nodes and/or S3 Tables (verified
    2026-08-13) — locations in those metros route to the nearest full-stack region
    and carry a distance note so the report flags the non-obvious choice."""
    tel_aviv = match_bq_location("me-west1")
    assert tel_aviv.aws_region == "eu-south-1"  # not il-central-1
    assert tel_aviv.kind == "nearest"
    assert "km" in tel_aviv.note  # distant match is flagged
    assert tel_aviv.ui_label == "distant region"

    doha = match_bq_location("me-central1")
    assert doha.aws_region == "ap-south-1"  # not me-central-1 / me-south-1
    assert doha.note != ""

    zurich = match_bq_location("europe-west6")
    assert zurich.aws_region == "eu-south-1"  # not eu-central-2
    assert zurich.note == "" and zurich.ui_label == ""  # Milan is close — no flag


def test_stack_regions_never_include_gapped_regions():
    for region in ("il-central-1", "me-central-1", "me-south-1", "eu-central-2", "ca-west-1"):
        assert region not in AWS_REGIONS_WITH_STACK


# ── Pins ───────────────────────────────────────────────────────────────


def test_multi_region_pins_are_quiet():
    """us/eu multi-region pins keep their note (methodology text) but suppress the
    badge — they're the common case and badging them breeds badge blindness."""
    us = match_bq_location("us")
    assert (us.aws_region, us.kind) == ("us-east-1", "pinned")
    assert us.note != "" and us.ui_label == ""
    eu = match_bq_location("eu")
    assert (eu.aws_region, eu.kind) == ("eu-west-1", "pinned")
    assert eu.note != "" and eu.ui_label == ""


def test_same_country_pins_beat_marginally_closer_cross_border():
    dallas = match_bq_location("us-south1")
    assert (dallas.aws_region, dallas.kind) == ("us-east-2", "pinned")
    assert dallas.ui_label == "pinned region"
    toronto = match_bq_location("northamerica-northeast2")
    assert (toronto.aws_region, toronto.kind) == ("ca-central-1", "pinned")
    assert toronto.ui_label == "pinned region"


def test_warsaw_pin():
    warsaw = match_bq_location("europe-central2")
    assert (warsaw.aws_region, warsaw.kind) == ("eu-central-1", "pinned")


def test_all_pins_target_full_stack_regions():
    for loc, (region, reason) in PINNED_BQ_LOCATIONS.items():
        assert region in AWS_REGIONS_WITH_STACK, f"pin {loc} → {region} lacks the stack"
        assert reason  # every pin must explain itself — the note reaches the report


# ── Fallback ───────────────────────────────────────────────────────────


def test_unknown_region_fallback():
    match = match_bq_location("mars-west1")
    assert (match.aws_region, match.kind) == ("us-east-1", "fallback")
    assert "--target-region" in match.note
    assert match.ui_label == "fallback region"


def test_none_and_empty_fallback():
    for loc in (None, "", "   "):
        match = match_bq_location(loc)
        assert (match.aws_region, match.kind) == ("us-east-1", "fallback")


def test_case_and_whitespace_insensitive():
    assert match_bq_location("US-EAST1").aws_region == "us-east-1"
    assert match_bq_location(" Asia-Southeast2 ").aws_region == "ap-southeast-3"


# ── Back-compat contract ───────────────────────────────────────────────


def test_bq_location_to_aws_region_two_tuple():
    assert bq_location_to_aws_region("asia-southeast2") == ("ap-southeast-3", False)
    assert bq_location_to_aws_region("us") == ("us-east-1", False)  # pinned ≠ fallback
    assert bq_location_to_aws_region("mars-west1") == ("us-east-1", True)
    assert bq_location_to_aws_region(None) == ("us-east-1", True)


def test_snapshot_dict_matches_policy():
    """BQ_LOCATION_TO_AWS_REGION is derived, never hand-edited — it must agree
    with match_bq_location for every location, including pins."""
    for loc in list(BQ_LOCATION_COORDS) + list(PINNED_BQ_LOCATIONS):
        assert BQ_LOCATION_TO_AWS_REGION[loc] == match_bq_location(loc).aws_region


# ── Ranking + geometry sanity ──────────────────────────────────────────


def test_nearest_aws_regions_ranked_and_stack_only():
    ranked = nearest_aws_regions("asia-southeast2")
    distances = [d for d, _ in ranked]
    assert distances == sorted(distances)
    assert ranked[0][1] == "ap-southeast-3" and ranked[0][0] < 50
    assert {r for _, r in ranked} <= AWS_REGIONS_WITH_STACK


def test_nearest_aws_regions_empty_for_unknown():
    assert nearest_aws_regions("us") == []  # multi-region has no coords
    assert nearest_aws_regions("nope") == []


def test_stack_regions_all_have_coords():
    assert AWS_REGIONS_WITH_STACK <= set(AWS_REGION_COORDS)


# ── Rates-table sync (the silent us-east-1 repricing bug) ──────────────


def test_every_mapped_region_has_verified_rates():
    """apply_aws_region must succeed for every region the policy can select —
    a mapped region without a rate row silently repriced customers at us-east-1."""
    from bq_assess.engine.redshift.cost_constants import (
        AWS_REGIONAL_RATES,
        V8_DT_OUT_REGIONAL_RATES,
    )
    mapped = set(BQ_LOCATION_TO_AWS_REGION.values())
    assert mapped <= set(AWS_REGIONAL_RATES)
    assert mapped <= set(V8_DT_OUT_REGIONAL_RATES)
    # and the rates table is exactly the verified-stack set — no drift either way
    assert set(AWS_REGIONAL_RATES) == set(AWS_REGIONS_WITH_STACK)
    assert set(V8_DT_OUT_REGIONAL_RATES) == set(AWS_REGIONS_WITH_STACK)
