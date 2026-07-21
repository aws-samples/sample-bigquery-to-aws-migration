"""BQ location → AWS region mapping (shared by collector and full tool)."""

from __future__ import annotations

BQ_LOCATION_TO_AWS_REGION: dict[str, str] = {
    # Multi-region
    "us": "us-east-1",
    "eu": "eu-west-1",
    # US regions
    "us-central1": "us-east-1",
    "us-east1": "us-east-1",
    "us-east4": "us-east-1",
    "us-east5": "us-east-1",
    "us-west1": "us-west-2",
    "us-west2": "us-west-2",
    "us-west3": "us-west-2",
    "us-west4": "us-west-2",
    "us-south1": "us-east-1",
    # North America
    "northamerica-northeast1": "ca-central-1",
    "northamerica-northeast2": "ca-central-1",
    # South America
    "southamerica-east1": "sa-east-1",
    "southamerica-west1": "sa-east-1",
    # Europe
    "europe-west1": "eu-west-1",
    "europe-west2": "eu-west-2",
    "europe-west3": "eu-central-1",
    "europe-west4": "eu-central-1",
    "europe-west6": "eu-central-2",
    "europe-west8": "eu-west-2",
    "europe-west9": "eu-west-3",
    "europe-west10": "eu-west-1",
    "europe-west12": "eu-central-1",
    "europe-central2": "eu-central-1",
    "europe-north1": "eu-north-1",
    "europe-north2": "eu-north-1",
    "europe-southwest1": "eu-south-2",
    # Asia
    "asia-east1": "ap-east-1",
    "asia-east2": "ap-east-1",
    "asia-northeast1": "ap-northeast-1",
    "asia-northeast2": "ap-northeast-2",
    "asia-northeast3": "ap-northeast-3",
    "asia-south1": "ap-south-1",
    "asia-south2": "ap-south-2",
    "asia-southeast1": "ap-southeast-1",
    "asia-southeast2": "ap-southeast-2",
    # Australia
    "australia-southeast1": "ap-southeast-2",
    "australia-southeast2": "ap-southeast-4",
    # Middle East
    "me-central1": "me-central-1",
    "me-central2": "me-central-1",
    "me-west1": "me-south-1",
    # Africa
    "africa-south1": "af-south-1",
}


def bq_location_to_aws_region(location: str | None) -> tuple[str, bool]:
    """Map a BigQuery dataset location to the nearest AWS region.

    Returns:
        Tuple of (aws_region, is_fallback).
        is_fallback=True when the location is unknown and us-east-1 fallback is used.
    """
    loc = (location or "").strip().lower()
    region = BQ_LOCATION_TO_AWS_REGION.get(loc)
    if region:
        return (region, False)
    return ("us-east-1", True)
