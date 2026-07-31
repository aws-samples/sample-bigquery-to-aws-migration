"""Single source for migration resource names derived from the BQ dataset.

Three artifacts must agree character-for-character: the Lambda/connector and
workgroup Terraform provisions, the CLI/data-catalog commands in the source-DB
setup, and the workgroup baked into run_migration.py. Deriving them here (and
only here) makes drift impossible instead of documented.
"""
from __future__ import annotations

import hashlib

# aws_s3_bucket.bucket_prefix hard limit (S3 caps names at 63; the provider reserves
# 26 for its uniqueness suffix).
_BUCKET_PREFIX_MAX = 37


def _safe_dataset(dataset_id: str) -> str:
    # Lowercased: S3 bucket names and the SAR connector's LambdaFunctionName
    # pattern (^[a-z0-9-_]{1,64}$) both reject uppercase.
    return dataset_id.replace("_", "-").lower()


def connector_name(dataset_id: str) -> str:
    """Athena federated data-source / connector Lambda name for a dataset."""
    return f"bq-connector-{_safe_dataset(dataset_id)}"


def workgroup_name(dataset_id: str) -> str:
    """Athena workgroup name Terraform provisions and run_migration.py targets."""
    return f"bq-migration-{_safe_dataset(dataset_id)}"


def table_bucket_name(dataset_id: str) -> str:
    """S3 Tables table-bucket name (3-63 chars, [a-z0-9-]).

    Account-scoped and created by terraform without a random suffix, so the
    name must be deterministic; same truncate+digest scheme as bucket_prefix.
    """
    friendly = f"bq-migration-{_safe_dataset(dataset_id)}"
    if len(friendly) <= 63:
        return friendly
    digest = hashlib.sha256(dataset_id.encode("utf-8")).hexdigest()[:6]
    keep = 63 - len(f"bq-migration--{digest}")
    slug = _safe_dataset(dataset_id)[:keep].rstrip("-")
    return f"bq-migration-{slug}-{digest}"


def namespace_name(dataset_id: str) -> str:
    """S3 Tables namespace (the Athena 'database') for a dataset.

    Namespaces allow [a-z0-9_]; keep the BQ dataset name verbatim (lowercased)
    so migrated tables keep their dataset-qualified identity.
    """
    return dataset_id.lower()


def bucket_prefix(dataset_id: str, purpose: str) -> str:
    """Length-safe aws_s3_bucket.bucket_prefix for a dataset + purpose (e.g. "iceberg").

    bucket_prefix is capped at 37 chars (63-char S3 name minus the provider's
    26-char uniqueness suffix); ``bq-migration-<dataset>-<purpose>-`` overflows it
    for dataset names longer than ~11 chars and fails ``terraform validate``.
    When the friendly form fits, keep it; otherwise truncate the dataset portion
    and splice in a short stable digest so distinct datasets can't collide after
    truncation. Deterministic — same inputs, same prefix.
    """
    friendly = f"bq-migration-{_safe_dataset(dataset_id)}-{purpose}-"
    if len(friendly) <= _BUCKET_PREFIX_MAX:
        return friendly
    digest = hashlib.sha256(dataset_id.encode("utf-8")).hexdigest()[:6]
    fixed = f"bq-migration--{digest}-{purpose}-"          # everything but the dataset slug
    keep = _BUCKET_PREFIX_MAX - len(fixed)
    slug = _safe_dataset(dataset_id)[:keep].rstrip("-")
    return f"bq-migration-{slug}-{digest}-{purpose}-"
