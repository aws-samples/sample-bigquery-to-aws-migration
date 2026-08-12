"""Generate Terraform files for Athena migration infrastructure.

Produces a self-contained terraform/ directory that provisions:
- Athena BigQuery Connector (Lambda deployed from the Serverless Application
  Repository via aws_serverlessapplicationrepository_cloudformation_stack —
  SAR resolves the connector code location and creates the Lambda's IAM role
  from the vendor's own SAM template; hand-building the Lambda against SAR's
  internal changeset buckets fails at apply time, they are not publicly readable)
- Glue federated database (maps 1:1 to BQ dataset)
- Athena workgroup with engine v3
- S3 results bucket
- Iceberg catalog in Glue

SAR app: AthenaGoogleBigQueryConnector (publisher 292517598671). Parameter names
verified against the app's SAM template (athena-google-bigquery.yaml, checked
2026-07-30, SAR version 2026.24.1): LambdaFunctionName, GCPProjectID,
SecretNamePrefix (→ env secret_manager_gcp_creds_name), SpillBucket, SpillPrefix.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from bq_assess.models import EntityMetadata


def generate_terraform(
    project_dir: str,
    dataset_id: str,
    gcp_project: str,
    tables: Sequence[EntityMetadata],
    target_region: str,
) -> str:
    """Write terraform/ directory under project_dir. Returns the terraform dir path."""
    tf_dir = Path(project_dir) / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)

    from bq_assess.engine.athena import naming
    connector_name = naming.connector_name(dataset_id)
    workgroup_name = naming.workgroup_name(dataset_id)
    # One S3 Tables namespace per SOURCE dataset: the generated DDL creates
    # tables as <dataset>.<table>, so every dataset present in the plan needs
    # its namespace to exist — not just the primary one the bucket is named for.
    all_namespaces = sorted({
        naming.namespace_name(t.dataset_id) for t in tables
    }) or [naming.namespace_name(dataset_id)]

    _write_variables(tf_dir, target_region, dataset_id, gcp_project, connector_name)
    _write_main(tf_dir, connector_name, workgroup_name, dataset_id, all_namespaces)
    _write_outputs(tf_dir)
    _write_tfvars_example(tf_dir, target_region, dataset_id, gcp_project, tables)

    return str(tf_dir)


def _write_variables(
    tf_dir: Path,
    target_region: str,
    dataset_id: str,
    gcp_project: str,
    connector_name: str,
) -> None:
    content = f'''variable "aws_region" {{
  description = "AWS region for all resources"
  type        = string
  default     = "{target_region}"
}}

variable "aws_account_id" {{
  description = "AWS account ID (used for Lambda ARN construction)"
  type        = string
}}

variable "gcp_project" {{
  description = "GCP project containing the BigQuery datasets"
  type        = string
  default     = "{gcp_project}"
}}

variable "dataset_id" {{
  description = "BigQuery dataset to migrate (lowercased, it becomes the S3 Tables namespace / Athena database for the Iceberg targets)"
  type        = string
  default     = "{dataset_id}"
}}

variable "connector_name" {{
  description = "Name for the Athena BigQuery Connector Lambda and data catalog"
  type        = string
  default     = "{connector_name}"
}}

variable "gcp_secret_name" {{
  description = "Secrets Manager secret NAME (not ARN) containing the GCP service-account JSON key"
  type        = string
}}

variable "spill_bucket" {{
  description = "S3 bucket for connector spill (large query intermediate results)"
  type        = string
  default     = ""
}}

variable "enable_s3tables_integration" {{
  description = "Create the account/region-wide s3tablescatalog Glue federated catalog. Set false if this account already has the S3 Tables analytics integration enabled (it is one per account/region)."
  type        = bool
  default     = true
}}

variable "migration_operator_principal_arn" {{
  description = "IAM principal ARN that will run run_migration.py (receives the Lake Formation grants). Leave empty to use the identity running terraform."
  type        = string
  default     = ""
}}

variable "tags" {{
  description = "Tags to apply to all resources"
  type        = map(string)
  default = {{
    Project   = "bq-migration"
    ManagedBy = "terraform"
  }}
}}
'''
    (tf_dir / "variables.tf").write_text(content, encoding="utf-8")


def _write_main(
    tf_dir: Path,
    connector_name: str,
    workgroup_name: str,
    dataset_id: str,
    all_namespaces: list[str] | None = None,
) -> None:
    from bq_assess.engine.athena import naming
    results_prefix = naming.bucket_prefix(dataset_id, "results")
    spill_prefix = naming.bucket_prefix(dataset_id, "spill")
    table_bucket_name = naming.table_bucket_name(dataset_id)
    namespace_name = naming.namespace_name(dataset_id)
    namespaces = all_namespaces or [namespace_name]
    namespaces_hcl = ", ".join(f'"{n}"' for n in namespaces)
    content = f'''terraform {{
  required_version = ">= 1.5"
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      # >= 6.40: aws_s3tables_* + aws_glue_catalog (federated_catalog block)
      version = ">= 6.40"
    }}
  }}
}}

provider "aws" {{
  region = var.aws_region
}}

# ─── S3: Query results bucket ────────────────────────────────────────────────

resource "aws_s3_bucket" "athena_results" {{
  bucket_prefix = "{results_prefix}"
  tags          = var.tags
}}

resource "aws_s3_bucket_public_access_block" "athena_results" {{
  bucket                  = aws_s3_bucket.athena_results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}

resource "aws_s3_bucket_server_side_encryption_configuration" "athena_results" {{
  bucket = aws_s3_bucket.athena_results.id
  # AES256 (SSE-S3) matches the workgroup's SSE_S3 result encryption — results
  # are transient (7-day expiry); the durable Iceberg/spill buckets stay on KMS.
  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "AES256"
    }}
  }}
}}

resource "aws_s3_bucket_lifecycle_configuration" "results_cleanup" {{
  bucket = aws_s3_bucket.athena_results.id

  rule {{
    id     = "expire-query-results"
    status = "Enabled"
    expiration {{
      days = 7
    }}
  }}
}}

# ─── S3: Connector spill bucket (for large federated queries) ────────────────

resource "aws_s3_bucket" "connector_spill" {{
  count         = var.spill_bucket == "" ? 1 : 0
  bucket_prefix = "{spill_prefix}"
  tags          = var.tags
}}

resource "aws_s3_bucket_public_access_block" "connector_spill" {{
  count                   = var.spill_bucket == "" ? 1 : 0
  bucket                  = aws_s3_bucket.connector_spill[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}

resource "aws_s3_bucket_server_side_encryption_configuration" "connector_spill" {{
  count  = var.spill_bucket == "" ? 1 : 0
  bucket = aws_s3_bucket.connector_spill[0].id
  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "aws:kms"
    }}
    bucket_key_enabled = true
  }}
}}

resource "aws_s3_bucket_lifecycle_configuration" "spill_cleanup" {{
  count  = var.spill_bucket == "" ? 1 : 0
  bucket = aws_s3_bucket.connector_spill[0].id

  rule {{
    id     = "expire-spill-data"
    status = "Enabled"
    expiration {{
      days = 3
    }}
  }}
}}

locals {{
  spill_bucket = var.spill_bucket != "" ? var.spill_bucket : aws_s3_bucket.connector_spill[0].id
}}

# ─── TLS-only bucket policies (deny any non-HTTPS access) ────────────────────

data "aws_iam_policy_document" "tls_only_results" {{
  statement {{
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {{
      type        = "*"
      identifiers = ["*"]
    }}
    resources = [
      aws_s3_bucket.athena_results.arn,
      "${{aws_s3_bucket.athena_results.arn}}/*",
    ]
    condition {{
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }}
  }}
}}

resource "aws_s3_bucket_policy" "tls_only_results" {{
  bucket     = aws_s3_bucket.athena_results.id
  policy     = data.aws_iam_policy_document.tls_only_results.json
  depends_on = [aws_s3_bucket_public_access_block.athena_results]
}}

data "aws_iam_policy_document" "tls_only_spill" {{
  count = var.spill_bucket == "" ? 1 : 0
  statement {{
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {{
      type        = "*"
      identifiers = ["*"]
    }}
    resources = [
      aws_s3_bucket.connector_spill[0].arn,
      "${{aws_s3_bucket.connector_spill[0].arn}}/*",
    ]
    condition {{
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }}
  }}
}}

resource "aws_s3_bucket_policy" "tls_only_spill" {{
  count      = var.spill_bucket == "" ? 1 : 0
  bucket     = aws_s3_bucket.connector_spill[0].id
  policy     = data.aws_iam_policy_document.tls_only_spill[0].json
  depends_on = [aws_s3_bucket_public_access_block.connector_spill]
}}

# ─── Athena BigQuery Connector (Lambda via Serverless Application Repository) ─
# Deploys the vendor-published connector app: SAR resolves the Lambda code
# location and the app's SAM template creates the function, its IAM role
# (Secrets Manager read + spill-bucket access), and its environment.
# Parameter names verified against athena-google-bigquery.yaml — identical in
# 2025.41.1 (the deployed version) and 2026.24.1.

resource "aws_serverlessapplicationrepository_cloudformation_stack" "bq_connector" {{
  name             = var.connector_name
  application_id   = "arn:aws:serverlessrepo:us-east-1:292517598671:applications/AthenaGoogleBigQueryConnector"
  # Pinned: every 2026.x image (through 2026.24.1) crashes on any query with
  # "Address types of NameResolver 'unix' ... not supported by transport"
  # (gRPC packaging regression). 2025.41.1 is the newest live-verified working
  # release (2026-07-30). Revisit when a fixed 2026.x ships.
  semantic_version = "2025.41.1"
  capabilities     = ["CAPABILITY_IAM", "CAPABILITY_RESOURCE_POLICY"]

  parameters = {{
    LambdaFunctionName = var.connector_name
    GCPProjectID       = var.gcp_project
    SecretNamePrefix   = var.gcp_secret_name
    SpillBucket        = local.spill_bucket
    SpillPrefix        = "athena-spill"
  }}

  tags = var.tags
}}

# ─── Athena Data Catalog (federated, points to the connector Lambda) ─────────

resource "aws_athena_data_catalog" "bigquery" {{
  name        = var.connector_name
  description = "Federated BigQuery connector for GCP project ${{var.gcp_project}} (all datasets; named after the primary dataset ${{var.dataset_id}})"
  type        = "LAMBDA"

  parameters = {{
    "function" = "arn:aws:lambda:${{var.aws_region}}:${{var.aws_account_id}}:function:${{var.connector_name}}"
  }}

  depends_on = [aws_serverlessapplicationrepository_cloudformation_stack.bq_connector]
}}

# ─── Athena Workgroup (engine v3, required for Iceberg DML) ──────────────────

resource "aws_athena_workgroup" "migration" {{
  name = "{workgroup_name}"

  configuration {{
    enforce_workgroup_configuration = true
    engine_version {{
      selected_engine_version = "Athena engine version 3"
    }}
    result_configuration {{
      output_location = "s3://${{aws_s3_bucket.athena_results.id}}/results/"
      encryption_configuration {{
        encryption_option = "SSE_S3"
      }}
    }}
  }}

  tags = var.tags
}}

# ─── S3 Tables: table bucket + namespace (Storage Target, ADR-0001) ──────────
# Managed Iceberg: automatic compaction, snapshot expiry, and unreferenced-file
# cleanup — no self-managed OPTIMIZE/VACUUM (Athena's are unsupported here).
#
# ⚠️ Intelligent-Tiering: the S3 Tables INT storage class (cost-neutral now,
# auto-discounts data unaccessed 30/90 days) is set at CREATION TIME ONLY and
# the terraform provider does not expose it yet. run_migration.py sets the
# bucket default via the PutTableBucketStorageClass API before creating tables;
# see MIGRATION_GUIDE.html. (Verified against the S3 Tables docs + API model,
# 2026-07-30.)

resource "aws_s3tables_table_bucket" "iceberg" {{
  name = "{table_bucket_name}"

  maintenance_configuration = {{
    iceberg_unreferenced_file_removal = {{
      status = "enabled"
      settings = {{
        unreferenced_days = 3
        non_current_days  = 10
      }}
    }}
  }}
}}

resource "aws_s3tables_namespace" "iceberg_target" {{
  for_each         = toset([{namespaces_hcl}])
  namespace        = each.value
  table_bucket_arn = aws_s3tables_table_bucket.iceberg.arn
}}

# ─── Glue catalog: the account/region S3 Tables integration ──────────────────
# ONE catalog named exactly "s3tablescatalog" serves ALL table buckets in the
# account/region (identifier is the bucket/* wildcard ARN — a slash in the
# catalog NAME is rejected by CreateCatalog; the "s3tablescatalog/<bucket>"
# form is how Athena ADDRESSES the per-bucket sub-catalogs it exposes).
# If this account already has the integration (e.g. enabled via the S3
# console), set enable_s3tables_integration = false.

resource "aws_glue_catalog" "s3tables_federated" {{
  count = var.enable_s3tables_integration ? 1 : 0
  name  = "s3tablescatalog"

  federated_catalog {{
    identifier      = "arn:aws:s3tables:${{var.aws_region}}:${{var.aws_account_id}}:bucket/*"
    connection_name = "aws:s3tables"
  }}

  depends_on = [aws_s3tables_table_bucket.iceberg]
}}

# ─── Lake Formation grants for the migration operator ────────────────────────
# The s3tablescatalog federated catalog is ALWAYS governed by Lake Formation —
# IAM alone is not enough. Without these grants every CREATE TABLE fails with
# "Insufficient Lake Formation permission(s)" (live-verified 2026-07-30), even
# for admin roles. Grants are per-namespace on the bucket's sub-catalog.

data "aws_caller_identity" "current" {{}}

locals {{
  # LF grants need an IAM principal, not an STS session: when terraform runs
  # under an assumed role, translate arn:aws:sts::acct:assumed-role/Name/sess
  # to arn:aws:iam::acct:role/Name. Override via migration_operator_principal_arn.
  lf_operator_principal = var.migration_operator_principal_arn != "" ? var.migration_operator_principal_arn : (
    length(regexall(":assumed-role/", data.aws_caller_identity.current.arn)) > 0
    ? "arn:aws:iam::${{var.aws_account_id}}:role/${{split("/", data.aws_caller_identity.current.arn)[1]}}"
    : data.aws_caller_identity.current.arn
  )
}}

resource "aws_lakeformation_permissions" "operator_db" {{
  for_each  = aws_s3tables_namespace.iceberg_target
  principal = local.lf_operator_principal

  permissions = ["CREATE_TABLE", "DESCRIBE"]

  database {{
    catalog_id = "${{var.aws_account_id}}:s3tablescatalog/{table_bucket_name}"
    name       = each.value.namespace
  }}
}}

resource "aws_lakeformation_permissions" "operator_tables" {{
  for_each  = aws_s3tables_namespace.iceberg_target
  principal = local.lf_operator_principal

  permissions = ["ALL"]

  table {{
    catalog_id    = "${{var.aws_account_id}}:s3tablescatalog/{table_bucket_name}"
    database_name = each.value.namespace
    wildcard      = true
  }}
}}

# ─── Least-privilege IAM policy for the migration operator ───────────────────
# Attach this policy to the IAM role/user that runs run_migration.py.
# It grants only what the migration needs (the Redshift Data API statement
# uses resources = ["*"] because those ARNs don't exist pre-provisioning).

data "aws_iam_policy_document" "migration_operator" {{
  statement {{
    sid = "AthenaRunQueries"
    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:StopQueryExecution",
      "athena:GetWorkGroup",
    ]
    resources = [aws_athena_workgroup.migration.arn]
  }}

  statement {{
    sid       = "AthenaFederatedCatalog"
    actions   = ["athena:GetDataCatalog"]
    resources = ["arn:aws:athena:${{var.aws_region}}:${{var.aws_account_id}}:datacatalog/${{var.connector_name}}"]
  }}

  statement {{
    sid       = "InvokeConnectorLambda"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${{var.aws_region}}:${{var.aws_account_id}}:function:${{var.connector_name}}"]
  }}

  # Athena resolves the federated s3tablescatalog through Glue — the operator
  # needs read on the catalog hierarchy (the multi-catalog path uses Get*).
  statement {{
    sid = "GlueFederatedCatalogRead"
    actions = [
      "glue:GetCatalog",
      "glue:GetCatalogs",
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:GetTable",
      "glue:GetTables",
    ]
    resources = [
      "arn:aws:glue:${{var.aws_region}}:${{var.aws_account_id}}:catalog",
      "arn:aws:glue:${{var.aws_region}}:${{var.aws_account_id}}:catalog/*",
      "arn:aws:glue:${{var.aws_region}}:${{var.aws_account_id}}:database/*",
      "arn:aws:glue:${{var.aws_region}}:${{var.aws_account_id}}:table/*/*",
    ]
  }}

  # DDL/DML on table buckets uses s3tables actions, NOT s3:PutObject
  # (action list per the Athena S3-Tables IAM docs, verified 2026-07-30).
  statement {{
    sid = "S3TablesTargetBucket"
    actions = [
      "s3tables:GetTableBucket",
      "s3tables:GetNamespace",
      "s3tables:ListNamespaces",
      "s3tables:GetTable",
      "s3tables:ListTables",
      "s3tables:CreateTable",
      "s3tables:DeleteTable",
      "s3tables:GetTableData",
      "s3tables:PutTableData",
      "s3tables:GetTableMetadataLocation",
      "s3tables:UpdateTableMetadataLocation",
      "s3tables:GetTableBucketStorageClass",
      "s3tables:PutTableBucketStorageClass",
    ]
    resources = [
      aws_s3tables_table_bucket.iceberg.arn,
      "${{aws_s3tables_table_bucket.iceberg.arn}}/table/*",
    ]
  }}

  statement {{
    sid = "S3ResultsBucket"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [
      aws_s3_bucket.athena_results.arn,
      "${{aws_s3_bucket.athena_results.arn}}/*",
    ]
  }}

  # Phase 3 (RMS tables via the Redshift Data API). ExecuteStatement against a
  # serverless workgroup with IAM temp auth additionally requires
  # redshift-serverless:GetCredentials (per API_ExecuteStatement docs).
  # ExecuteStatement supports resource-level scoping to the workgroup ARN;
  # Describe/Cancel are statement-scoped (no resource ARN exists) so they are
  # conditioned to statements THIS principal submitted.
  statement {{
    sid       = "RedshiftDataApiExecute"
    actions   = ["redshift-data:ExecuteStatement"]
    resources = ["arn:aws:redshift-serverless:${{var.aws_region}}:${{var.aws_account_id}}:workgroup/*"]
  }}

  statement {{
    sid = "RedshiftDataApiOwnStatements"
    actions = [
      "redshift-data:DescribeStatement",
      "redshift-data:CancelStatement",
    ]
    resources = ["*"]
    condition {{
      test     = "StringEquals"
      variable = "redshift-data:statement-owner-iam-userid"
      values   = ["$${{aws:userid}}"]
    }}
  }}

  statement {{
    sid       = "RedshiftServerlessTempAuth"
    actions   = ["redshift-serverless:GetCredentials"]
    resources = ["arn:aws:redshift-serverless:${{var.aws_region}}:${{var.aws_account_id}}:workgroup/*"]
  }}

  # Phase 3 creates one Glue resource link per RMS dataset in the DEFAULT
  # catalog (name suffix _rl) — scoped so the operator can create/read links
  # but cannot touch other Glue databases.
  statement {{
    sid     = "GlueResourceLinksPhase3"
    actions = ["glue:CreateDatabase"]
    resources = [
      "arn:aws:glue:${{var.aws_region}}:${{var.aws_account_id}}:catalog",
      "arn:aws:glue:${{var.aws_region}}:${{var.aws_account_id}}:database/*_rl",
    ]
  }}
}}

resource "aws_iam_policy" "migration_operator" {{
  name        = "${{var.connector_name}}-operator"
  description = "Least-privilege policy for running the BQ-to-Iceberg migration (run_migration.py)"
  policy      = data.aws_iam_policy_document.migration_operator.json
  tags        = var.tags
}}
'''
    (tf_dir / "main.tf").write_text(content, encoding="utf-8")


def _write_outputs(tf_dir: Path) -> None:
    content = '''output "connector_name" {
  description = "Athena federated data catalog name (use in SQL as the catalog prefix)"
  value       = aws_athena_data_catalog.bigquery.name
}

output "workgroup_name" {
  description = "Athena workgroup configured for engine v3 (use for all migration queries)"
  value       = aws_athena_workgroup.migration.name
}

output "results_bucket" {
  description = "S3 bucket for Athena query results"
  value       = aws_s3_bucket.athena_results.id
}

output "table_bucket_name" {
  description = "S3 Tables table bucket holding the Iceberg data"
  value       = aws_s3tables_table_bucket.iceberg.name
}

output "table_bucket_arn" {
  description = "ARN of the S3 Tables table bucket (needed for put-table-bucket-storage-class)"
  value       = aws_s3tables_table_bucket.iceberg.arn
}

output "iceberg_catalog" {
  description = "Athena catalog for the Iceberg targets (s3tablescatalog/<bucket>)"
  value       = "s3tablescatalog/${aws_s3tables_table_bucket.iceberg.name}"
}

output "iceberg_database" {
  description = "Primary S3 Tables namespace (Athena database) for Iceberg target tables"
  value       = lower(var.dataset_id)
}

output "iceberg_namespaces" {
  description = "All namespaces (Athena databases) created in the table bucket"
  value       = [for ns in aws_s3tables_namespace.iceberg_target : ns.namespace]
}

output "connector_lambda_arn" {
  description = "ARN of the BigQuery connector Lambda"
  value       = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:${var.connector_name}"
}

output "migration_operator_policy_arn" {
  description = "Least-privilege IAM policy for the identity that runs run_migration.py — attach with: aws iam attach-role-policy (or attach-user-policy)"
  value       = aws_iam_policy.migration_operator.arn
}
'''
    (tf_dir / "outputs.tf").write_text(content, encoding="utf-8")


def _write_tfvars_example(
    tf_dir: Path,
    target_region: str,
    dataset_id: str,
    gcp_project: str,
    tables: Sequence[EntityMetadata],
) -> None:
    table_count = len([t for t in tables])
    total_gb = sum(t.num_bytes for t in tables) / (1024**3)

    content = f'''# ─── Required: fill these in ──────────────────────────────────────────────────

aws_account_id  = "123456789012"  # Your AWS account ID
gcp_secret_name = "gcp-bigquery-sa"  # Secrets Manager secret NAME (no random suffix, not the ARN)

# ─── Pre-filled from assessment (edit if needed) ─────────────────────────────

aws_region     = "{target_region}"
gcp_project    = "{gcp_project}"
dataset_id     = "{dataset_id}"

# Assessment summary: {table_count} tables, {total_gb:.1f} GB total

# ─── Optional ────────────────────────────────────────────────────────────────

# spill_bucket = "my-existing-spill-bucket"  # Leave empty to create a new one

# Lake Formation grants default to the identity running terraform. If a
# DIFFERENT role runs run_migration.py, put that role's ARN here so the
# grants land on the right principal (see MIGRATION_GUIDE, Step 4):
# migration_operator_principal_arn = "arn:aws:iam::123456789012:role/YOUR_MIGRATION_ROLE"

# tags = {{
#   Project     = "bq-migration"
#   Environment = "production"
#   CostCenter  = "data-platform"
# }}
'''
    (tf_dir / "terraform.tfvars.example").write_text(content, encoding="utf-8")
