"""Tests for BigQuery location → AWS region mapping."""
from __future__ import annotations

from bq_assess.core.region_mapping import bq_location_to_aws_region


def test_known_us_regions():
    """MRI-4: Known US regions should map correctly"""
    assert bq_location_to_aws_region("us-east1") == ("us-east-1", False)
    assert bq_location_to_aws_region("us-east4") == ("us-east-1", False)
    assert bq_location_to_aws_region("us-west1") == ("us-west-2", False)
    assert bq_location_to_aws_region("us-west2") == ("us-west-2", False)


def test_known_europe_regions():
    """MRI-4: Known Europe regions should map correctly"""
    assert bq_location_to_aws_region("europe-west1") == ("eu-west-1", False)
    assert bq_location_to_aws_region("europe-west2") == ("eu-west-2", False)
    # MRI-4: europe-west6 (Zurich) → eu-central-2
    assert bq_location_to_aws_region("europe-west6") == ("eu-central-2", False)
    assert bq_location_to_aws_region("europe-north1") == ("eu-north-1", False)


def test_known_asia_regions():
    """MRI-4: Known Asia regions should map correctly"""
    assert bq_location_to_aws_region("asia-east1") == ("ap-east-1", False)
    assert bq_location_to_aws_region("asia-northeast1") == ("ap-northeast-1", False)
    assert bq_location_to_aws_region("asia-south1") == ("ap-south-1", False)
    assert bq_location_to_aws_region("asia-southeast1") == ("ap-southeast-1", False)


def test_known_australia_regions():
    """MRI-4: Known Australia regions should map correctly"""
    assert bq_location_to_aws_region("australia-southeast1") == ("ap-southeast-2", False)
    assert bq_location_to_aws_region("australia-southeast2") == ("ap-southeast-4", False)


def test_known_middle_east_regions():
    """MRI-4: Known Middle East regions should map correctly"""
    assert bq_location_to_aws_region("me-central1") == ("me-central-1", False)
    assert bq_location_to_aws_region("me-west1") == ("me-south-1", False)


def test_known_africa_regions():
    """MRI-4: Known Africa regions should map correctly"""
    assert bq_location_to_aws_region("africa-south1") == ("af-south-1", False)


def test_multi_regions():
    """MRI-4: Multi-regions should map correctly"""
    assert bq_location_to_aws_region("us") == ("us-east-1", False)
    assert bq_location_to_aws_region("eu") == ("eu-west-1", False)


def test_unknown_region_fallback():
    """MRI-4: Unknown regions should return us-east-1 with fallback flag"""
    aws_region, is_fallback = bq_location_to_aws_region("mars-west1")
    assert aws_region == "us-east-1"
    assert is_fallback is True


def test_none_location_fallback():
    """MRI-4: None location should return us-east-1 with fallback flag"""
    aws_region, is_fallback = bq_location_to_aws_region(None)
    assert aws_region == "us-east-1"
    assert is_fallback is True


def test_empty_string_fallback():
    """MRI-4: Empty string should return us-east-1 with fallback flag"""
    aws_region, is_fallback = bq_location_to_aws_region("")
    assert aws_region == "us-east-1"
    assert is_fallback is True


def test_case_insensitive():
    """MRI-4: Location matching should be case-insensitive"""
    assert bq_location_to_aws_region("US-EAST1") == ("us-east-1", False)
    assert bq_location_to_aws_region("Europe-West1") == ("eu-west-1", False)
