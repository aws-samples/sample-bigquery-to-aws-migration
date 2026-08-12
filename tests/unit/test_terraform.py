"""Terraform generation: deep-audit IAM + connector fixes (2026-07-22),
SAR connector deploy + bucket_prefix length fix (2026-07-30)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from bq_assess.engine.athena import naming
from bq_assess.engine.athena.terraform import generate_terraform

# Long enough that the friendly bucket_prefix form would exceed the 37-char cap.
_LONG_DATASET = "verylong_analytics_warehouse_prod"


def _tf(tmp_path, dataset_id="my_ds"):
    generate_terraform(
        project_dir=str(tmp_path), dataset_id=dataset_id, gcp_project="proj",
        tables=[], target_region="ap-southeast-2",
    )
    return (tmp_path / "terraform" / "main.tf").read_text()


def test_operator_policy_covers_phase3(tmp_path):
    """Deep audit HRI-1: the operator policy must grant the redshift-data actions
    Phase 3 calls, plus redshift-serverless:GetCredentials for IAM temp auth."""
    tf = _tf(tmp_path)
    assert "redshift-data:ExecuteStatement" in tf
    assert "redshift-data:DescribeStatement" in tf
    assert "redshift-data:CancelStatement" in tf
    assert "redshift-serverless:GetCredentials" in tf


def test_operator_policy_covers_s3tables(tmp_path):
    """DDL/DML on table buckets uses s3tables actions (not s3:PutObject);
    DeleteTable covers run_migration.py re-runs; storage-class actions cover
    the Intelligent-Tiering bootstrap."""
    tf = _tf(tmp_path)
    for action in ("s3tables:CreateTable", "s3tables:DeleteTable",
                   "s3tables:GetTableData", "s3tables:PutTableData",
                   "s3tables:UpdateTableMetadataLocation",
                   "s3tables:PutTableBucketStorageClass"):
        assert action in tf, f"missing operator action {action}"


def test_connector_deploys_from_sar(tmp_path):
    """The connector must come from the Serverless Application Repository — the
    hand-built CFN Lambda pointed at SAR's internal changeset buckets, which are
    not publicly readable, so apply always failed (verified 2026-07-30)."""
    tf = _tf(tmp_path)
    assert "aws_serverlessapplicationrepository_cloudformation_stack" in tf
    assert "applications/AthenaGoogleBigQueryConnector" in tf
    # The fabricated code path and hand-rolled function must be gone.
    assert "awsserverlessrepo-changesets" not in tf
    assert "AWS::Lambda::Function" not in tf
    # SAM-template parameter names (athena-google-bigquery.yaml, SAR 2026.24.1).
    for param in ("LambdaFunctionName", "GCPProjectID", "SecretNamePrefix",
                  "SpillBucket", "SpillPrefix"):
        assert param in tf, f"missing SAR parameter {param}"


def test_s3_tables_storage_target(tmp_path):
    """Iceberg data lives in an S3 Tables table bucket (ADR-0001), exposed to
    Athena via the s3tablescatalog federated Glue catalog; the old GP iceberg
    bucket is gone. Unreferenced-file removal (managed maintenance) enabled."""
    tf = _tf(tmp_path)
    assert "aws_s3tables_table_bucket" in tf
    assert "aws_s3tables_namespace" in tf
    assert 'connection_name = "aws:s3tables"' in tf
    assert "s3tablescatalog/" in tf
    assert "iceberg_unreferenced_file_removal" in tf
    # The GP-bucket iceberg resources must be gone (results/spill GP buckets stay).
    assert 'resource "aws_s3_bucket" "iceberg_data"' not in tf
    assert "iceberg_intelligent_tiering" not in tf  # replaced by the API bootstrap


def test_bucket_prefix_within_provider_limit(tmp_path):
    """bucket_prefix caps at 37 chars; long dataset names must still validate."""
    tf = _tf(tmp_path, dataset_id=_LONG_DATASET)
    prefixes = [
        line.split('"')[1]
        for line in tf.splitlines()
        if "bucket_prefix" in line and '"' in line
    ]
    assert len(prefixes) == 2  # results, spill (iceberg is a table bucket now)
    for p in prefixes:
        assert len(p) <= 37, f"bucket_prefix too long ({len(p)}): {p}"
    # Table bucket name obeys its own 63-char cap.
    assert len(naming.table_bucket_name(_LONG_DATASET)) <= 63


def test_bucket_prefix_helper_properties():
    """Deterministic, purpose-distinct, and collision-resistant after truncation."""
    a = naming.bucket_prefix(_LONG_DATASET, "iceberg")
    assert a == naming.bucket_prefix(_LONG_DATASET, "iceberg")
    assert a != naming.bucket_prefix(_LONG_DATASET, "results")
    # Two datasets that truncate to the same slug must not collide.
    b = naming.bucket_prefix(_LONG_DATASET + "_v2", "iceberg")
    assert a != b
    # Short names keep the friendly form.
    assert naming.bucket_prefix("sales", "iceberg") == "bq-migration-sales-iceberg-"


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
def test_generated_terraform_validates(tmp_path):
    """Regression gate: `terraform validate` on generated output for a LONG dataset
    name (catches provider-level constraint violations plan-time checks miss)."""
    generate_terraform(
        project_dir=str(tmp_path), dataset_id=_LONG_DATASET, gcp_project="proj",
        tables=[], target_region="us-east-1",
    )
    tf_dir = tmp_path / "terraform"
    init = subprocess.run(
        ["terraform", "init", "-backend=false", "-input=false"],
        cwd=tf_dir, capture_output=True, text=True, timeout=300, check=False,
    )
    assert init.returncode == 0, init.stderr
    validate = subprocess.run(
        ["terraform", "validate"],
        cwd=tf_dir, capture_output=True, text=True, timeout=60, check=False,
    )
    assert validate.returncode == 0, validate.stdout + validate.stderr


def _entity(dataset_id, name="t1"):
    from datetime import datetime, timezone

    import bq_assess.models as m
    return m.EntityMetadata(
        entity_id=name,
        dataset_id=dataset_id,
        full_name=f"{dataset_id}.{name}",
        entity_type=m.EntityType.TABLE,
        population=m.EntityPopulation.TABLE,
        num_rows=1,
        num_bytes=1,
        columns=[],
        time_partitioning=None,
        range_partitioning=None,
        clustering_fields=None,
        view_query=None,
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_namespace_per_dataset(tmp_path):
    """Every SOURCE dataset in the plan gets its own S3 Tables namespace —
    the DDL creates <dataset>.<table>, so a missing namespace fails Phase 1
    with 'Cannot find or access the specified table' (live-verified 2026-07-30)."""
    generate_terraform(
        project_dir=str(tmp_path), dataset_id="ds_a", gcp_project="proj",
        tables=[_entity("ds_a"), _entity("ds_b"), _entity("ds_c", "t2")],
        target_region="ap-southeast-2",
    )
    tf = (tmp_path / "terraform" / "main.tf").read_text()
    assert 'for_each         = toset(["ds_a", "ds_b", "ds_c"])' in tf


def test_namespace_falls_back_to_primary_dataset(tmp_path):
    """No table entities (e.g. views-only source) still creates the primary
    dataset's namespace."""
    tf = _tf(tmp_path)  # tables=[]
    assert 'toset(["my_ds"])' in tf


def test_lakeformation_grants_emitted(tmp_path):
    """The s3tablescatalog federated catalog is governed by Lake Formation —
    without per-namespace grants every CREATE TABLE fails with 'Insufficient
    Lake Formation permission(s)' even for admin roles (live-verified 2026-07-30)."""
    tf = _tf(tmp_path)
    assert "aws_lakeformation_permissions" in tf
    assert '"CREATE_TABLE", "DESCRIBE"' in tf.replace("'", '"')
    # table-level ALL grant with wildcard
    assert "wildcard      = true" in tf
    # assumed-role sessions must be mapped back to their IAM role for LF
    assert ":assumed-role/" in tf
    # operator override variable exists
    variables = (tmp_path / "terraform" / "variables.tf").read_text()
    assert "migration_operator_principal_arn" in variables


def test_operator_policy_least_privilege(tmp_path):
    """No unscoped wildcard resources except the owner-conditioned Data API
    Describe/Cancel statement (those APIs have no resource ARN); Phase 3's
    glue:CreateDatabase is confined to *_rl resource links."""
    tf = _tf(tmp_path)
    # ExecuteStatement must be workgroup-scoped, not "*"
    assert 'sid       = "RedshiftDataApiExecute"' in tf
    exec_block = tf.split('sid       = "RedshiftDataApiExecute"')[1][:300]
    assert "workgroup/*" in exec_block
    # Describe/Cancel wildcard must carry the statement-owner condition
    own_block = tf.split('sid = "RedshiftDataApiOwnStatements"')[1][:500]
    assert "statement-owner-iam-userid" in own_block
    # Resource links: CreateDatabase confined to the _rl suffix
    rl_block = tf.split('sid     = "GlueResourceLinksPhase3"')[1][:400]
    assert "database/*_rl" in rl_block
    assert 'actions = ["glue:CreateDatabase"]' in rl_block
