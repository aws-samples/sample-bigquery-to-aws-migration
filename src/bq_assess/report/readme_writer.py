"""Customer-facing README.html generator.

Writes a top-level README at the project output root that guides the customer
through the full deliverable: what it is, how to read it, required permissions,
and a quick-start reading order.
"""
from __future__ import annotations

from pathlib import Path


def write_readme(
    project_dir: str,
    gcp_project: str,
    has_report: bool = True,
    has_terraform: bool = True,
    has_migration: bool = True,
    has_bundle: bool = True,
    has_rebuilt_entities: bool = False,
    has_redshift_phase: bool = False,
) -> str:
    """Write README.html at the project output root. Returns the file path."""
    out = Path(project_dir) / "README.html"
    out.write_text(_render(
        gcp_project=gcp_project,
        has_report=has_report,
        has_terraform=has_terraform,
        has_migration=has_migration,
        has_bundle=has_bundle,
        has_rebuilt_entities=has_rebuilt_entities,
        has_redshift_phase=has_redshift_phase,
    ), encoding="utf-8")
    return str(out)


def _render(
    gcp_project: str,
    has_report: bool,
    has_terraform: bool,
    has_migration: bool,
    has_bundle: bool,
    has_rebuilt_entities: bool,
    has_redshift_phase: bool,
) -> str:
    report_file = f"{gcp_project}-assessment.html"

    dir_rows = []
    if has_report:
        dir_rows.append(
            f"<tr><td><code>report/</code></td>"
            f"<td>Assessment report (HTML &amp; JSON) — open <code>{report_file}</code> in a browser</td></tr>"
        )
    if has_terraform:
        dir_rows.append(
            "<tr><td><code>terraform/</code></td>"
            "<td>Infrastructure-as-Code — deploys the Athena workgroup, BigQuery connector, "
            "S3 buckets, and catalog into your AWS account</td></tr>"
        )
    if has_migration:
        migration_files = "<code>plan.json</code>, <code>run_migration.py</code>, <code>MIGRATION_GUIDE.html</code>"
        if has_rebuilt_entities:
            migration_files += ", <code>rebuilt_entities.sql</code>"
        if has_redshift_phase:
            migration_files += ", <code>redshift_phase.sql</code>"
        dir_rows.append(
            f"<tr><td><code>migration/</code></td>"
            f"<td>Executable migration scripts ({migration_files}) — "
            f"reads from BigQuery, writes to S3 Tables (Iceberg)</td></tr>"
        )
    if has_bundle:
        dir_rows.append(
            "<tr><td><code>bundle/</code></td>"
            "<td>Assessment data export — re-processable by <code>bq-assess report</code> "
            "to regenerate reports without re-scanning GCP</td></tr>"
        )

    dir_table = "\n      ".join(dir_rows)

    # Quick-start steps: built as a list so numbering stays contiguous
    # regardless of which optional deliverables exist.
    steps = [
        (
            f"<strong>Open <code>report/{report_file}</code></strong> in a browser &mdash; "
            "this is the assessment. It has two tabs:"
            "<ul>"
            "<li><strong>Migration Effort</strong> &mdash; how hard it is to move each entity&rsquo;s "
            "<em>data</em> (schema, partitioning, size)</li>"
            "<li><strong>Query Complexity</strong> &mdash; how hard it is to rewrite the <em>SQL</em> "
            "that runs on it (views, routines, query logs)</li>"
            "</ul>"
        ),
    ]
    if has_migration:
        steps.append(
            "<strong>Review <code>migration/MIGRATION_GUIDE.html</code></strong> &mdash; "
            "step-by-step instructions for deploying infrastructure and running the migration."
        )
    if has_terraform:
        steps.append(
            "<strong>Deploy <code>terraform/</code></strong> into your AWS account "
            "(see MIGRATION_GUIDE Step 4)."
        )
    if has_migration:
        steps.append(
            "<strong>Run <code>migration/run_migration.py</code></strong> to load your data "
            "(Phase 1: DDL, Phase 2: data load)."
        )
    if has_rebuilt_entities:
        steps.append(
            "<strong>Apply <code>migration/rebuilt_entities.sql</code></strong> &mdash; "
            "recreates views, routines, and materialized-view alternatives on the target engine."
        )
    if has_redshift_phase:
        steps.append(
            "<strong>Run <code>migration/redshift_phase.sql</code></strong> in Redshift "
            "for tables placed on Redshift Managed Storage."
        )
    quick_start = "\n    ".join(
        f'<li><span class="step-num">{i}</span> {body}</li>'
        for i, body in enumerate(steps, 1)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>README — BigQuery Migration Assessment</title>
  <style>
    :root {{
      --color-bg-layout: #f2f8fd;
      --color-bg-default: #ffffff;
      --color-bg-header: #0f1b2a;
      --color-text-body: #000716;
      --color-text-heading: #0f1b2a;
      --color-text-secondary: #5f6b7a;
      --color-text-muted: #7d8998;
      --color-text-inverse: #ffffff;
      --color-text-link: #0972d3;
      --color-border-divider: #e9ebed;
      --color-border-card: #e9ebed;
      --color-brand-orange: #ff9900;
      --color-severity-success: #037f0c;
      --color-severity-success-bg: #f2fcf3;
      --color-severity-warning: #d97706;
      --color-severity-warning-bg: #fffce9;
      --color-severity-error: #d91515;
      --color-severity-error-bg: #fff7f7;
      --color-severity-info: #0972d3;
      --color-severity-info-bg: #f0f9ff;
      --shadow-xs: 0 1px 2px 0 rgba(0, 7, 22, 0.05);
      --radius-sm: 8px;
      --radius-md: 12px;
      --font-body: "Amazon Ember", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-mono: "Source Code Pro", "Fira Code", ui-monospace, SFMono-Regular, Menlo, monospace;
      --space-xs: 4px; --space-sm: 8px; --space-md: 16px;
      --space-lg: 24px; --space-xl: 32px;
    }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: var(--font-body); font-size: 14px; line-height: 1.6;
      color: var(--color-text-body); background: var(--color-bg-layout);
      margin: 0; padding: 0; -webkit-font-smoothing: antialiased;
    }}
    .container {{ max-width: 960px; margin: 0 auto; padding: 0 var(--space-lg); }}
    h1, h2, h3 {{ margin-top: 0; font-weight: 700; line-height: 1.25; color: var(--color-text-heading); }}
    h1 {{ font-size: 1.5rem; }}
    h2 {{ font-size: 1.125rem; margin: var(--space-xl) 0 var(--space-md); }}
    h3 {{ font-size: 0.9375rem; margin-bottom: var(--space-md); }}
    p {{ margin-top: 0; margin-bottom: var(--space-md); }}
    li {{ margin-bottom: var(--space-sm); }}
    ul, ol {{ padding-left: 1.5rem; margin-top: 0; }}
    a {{ color: var(--color-text-link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{
      font-family: var(--font-mono); font-size: 0.8125rem;
      background: #f4f4f4; padding: 0.15rem 0.4rem; border-radius: 4px;
    }}
    pre {{
      background: var(--color-bg-header); color: #d5dbdb;
      padding: var(--space-md); border-radius: var(--radius-sm);
      overflow-x: auto; margin: var(--space-md) 0;
      font-size: 0.8125rem; font-family: var(--font-mono);
    }}
    pre code {{ background: none; padding: 0; color: inherit; }}
    .header {{ background: var(--color-bg-header); }}
    .header-inner {{ display: flex; align-items: center; padding: var(--space-md) 0; min-height: 56px; }}
    .header-logo {{ display: flex; align-items: center; gap: var(--space-sm); color: var(--color-text-inverse); font-weight: 700; font-size: 0.9375rem; }}
    .header-logo .aws-cube {{
      width: 32px; height: 32px; background: var(--color-brand-orange); border-radius: 6px;
      display: flex; align-items: center; justify-content: center;
      font-weight: 800; font-size: 11px; color: var(--color-bg-header); letter-spacing: 0.5px;
    }}
    .content {{ padding: var(--space-xl) 0; }}
    .card {{
      background: var(--color-bg-default); border: 1px solid var(--color-border-card);
      border-radius: var(--radius-md); padding: var(--space-lg);
      margin: var(--space-md) 0; box-shadow: var(--shadow-xs);
    }}
    .warning {{
      background: var(--color-severity-warning-bg); border: 1px solid var(--color-severity-warning);
      border-radius: var(--radius-sm); padding: var(--space-md); margin: var(--space-md) 0;
    }}
    .warning h3 {{ color: var(--color-severity-warning); margin-bottom: var(--space-sm); }}
    .info {{
      background: var(--color-severity-info-bg); border: 1px solid var(--color-severity-info);
      border-radius: var(--radius-sm); padding: var(--space-md); margin: var(--space-md) 0;
    }}
    .info h3 {{ color: var(--color-severity-info); margin-bottom: var(--space-sm); }}
    .success {{
      background: var(--color-severity-success-bg); border: 1px solid var(--color-severity-success);
      border-radius: var(--radius-sm); padding: var(--space-md); margin: var(--space-md) 0;
    }}
    .success h3 {{ color: var(--color-severity-success); margin-bottom: var(--space-sm); }}
    table {{ width: 100%; border-collapse: collapse; margin: var(--space-md) 0; background: var(--color-bg-default); border-radius: var(--radius-sm); overflow: hidden; box-shadow: var(--shadow-xs); }}
    th, td {{ border-bottom: 1px solid var(--color-border-divider); padding: 0.625rem 0.75rem; text-align: left; font-size: 0.8125rem; }}
    th {{ background: #f4f4f4; font-weight: 600; color: var(--color-text-secondary); text-transform: uppercase; letter-spacing: 0.4px; font-size: 0.6875rem; }}
    tr:last-child td {{ border-bottom: none; }}
    .subtitle {{ color: var(--color-text-secondary); font-size: 0.9375rem; margin: 0 0 var(--space-lg); }}
    .step-num {{
      display: inline-flex; align-items: center; justify-content: center;
      background: var(--color-text-link); color: #fff;
      width: 24px; height: 24px; border-radius: 50%;
      font-weight: 700; font-size: 0.75rem; margin-right: var(--space-sm);
      flex-shrink: 0;
    }}
    .footer {{ margin-top: var(--space-xl); padding: var(--space-lg) 0; border-top: 1px solid var(--color-border-divider); font-size: 0.75rem; color: var(--color-text-muted); }}
  </style>
</head>
<body>

<div class="header">
  <div class="container">
    <div class="header-inner">
      <div class="header-logo">
        <div class="aws-cube">BQ</div>
        <div>
          <h1 style="color:#fff;margin:0;font-size:1.125rem">Migration Assessment — README</h1>
          <p style="color:rgba(255,255,255,.5);margin:2px 0 0;font-size:.75rem">{gcp_project}</p>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="content">
<div class="container">

<p class="subtitle">This package contains the full output of the BigQuery &rarr; AWS migration assessment.
Start by reading the assessment report, then use the terraform and migration scripts to execute.</p>

<!-- ═══════════════ WHAT'S INSIDE ═══════════════ -->

<h2>What&rsquo;s in This Package</h2>

<table>
  <thead><tr><th>Directory</th><th>Contents</th></tr></thead>
  <tbody>
    {dir_table}
  </tbody>
</table>

<!-- ═══════════════ QUICK START ═══════════════ -->

<h2>Quick-Start Reading Order</h2>

<div class="card">
  <ol>
    {quick_start}
  </ol>
</div>

<!-- ═══════════════ PERMISSIONS ═══════════════ -->

<h2>GCP Permissions Required</h2>

<p>The assessment tool reads metadata (not table contents) from your BigQuery project.
The following permissions must be granted to the principal running the tool (service account or user via ADC).</p>

<div class="warning">
  <h3>Grant these before running the tool</h3>
  <p>Missing permissions cause specific features to degrade gracefully (the tool still runs), but
  you&rsquo;ll get incomplete results. Grant all of them for a complete assessment.</p>
</div>

<table>
  <thead><tr><th>Permission / Role</th><th>What It Enables</th><th>Error If Missing</th></tr></thead>
  <tbody>
    <tr>
      <td><code>bigquery.tables.get</code><br><code>bigquery.tables.list</code><br><br>
        <em>Role:</em> <code>roles/bigquery.metadataViewer</code><br>
        (or <code>roles/bigquery.dataViewer</code>)</td>
      <td>Scanning table schemas, column types, partitioning, clustering &mdash; the core assessment.
        Also required for <code>INFORMATION_SCHEMA.TABLE_STORAGE</code> (physical size measurement).</td>
      <td><code>403 Access Denied: User does not have the required permissions
        ('bigquery.tables.get', 'bigquery.tables.list' permission(s) at the project level)
        to query system entity ... INFORMATION_SCHEMA.TABLE_STORAGE</code></td>
    </tr>
    <tr>
      <td><code>bigquery.jobs.listAll</code><br><br>
        <em>Role:</em> <code>roles/bigquery.resourceViewer</code></td>
      <td>Reading query logs from <code>INFORMATION_SCHEMA.JOBS_BY_PROJECT</code>.
        Enables workload analysis, query complexity scoring from real usage patterns,
        and pricing-model detection (on-demand vs capacity).</td>
      <td><code>Missing required permission 'bigquery.jobs.listAll' on project</code><br>
        <em>Without this:</em> query complexity scores drop to LOW confidence; workload
        and cost analysis are unavailable.</td>
    </tr>
    <tr>
      <td><code>bigquery.reservations.list</code><br>
        <code>bigquery.capacityCommitments.list</code><br>
        <code>bigquery.reservationAssignments.list</code><br><br>
        <em>Role:</em> <code>roles/bigquery.resourceViewer</code></td>
      <td>Reading reservation/commitment details from <code>INFORMATION_SCHEMA.RESERVATIONS</code>,
        <code>CAPACITY_COMMITMENTS</code>, <code>ASSIGNMENTS</code>. Required for enterprise
        capacity cost modelling.</td>
      <td><code>403 Forbidden</code> when querying reservation views.<br>
        <em>Without this:</em> capacity pricing detected from JOBS but detailed slot/commitment
        figures unavailable (pass <code>--reservation-config</code> as a workaround).</td>
    </tr>
    <tr>
      <td><code>bigquery.jobs.create</code><br><br>
        <em>Role:</em> <code>roles/bigquery.jobUser</code></td>
      <td>Running the INFORMATION_SCHEMA queries themselves (all queries require a job).</td>
      <td><code>403 Access Denied: User does not have bigquery.jobs.create permission</code></td>
    </tr>
    <tr>
      <td><code>bigquery.readsessions.create</code><br><br>
        <em>Role:</em> <code>roles/bigquery.readSessionUser</code></td>
      <td>Required by the <strong>Athena BigQuery Connector</strong> during migration execution
        (not during assessment). The connector uses the Storage Read API to fetch table data.</td>
      <td><code>PERMISSION_DENIED: Request had insufficient authentication scopes</code>
        on every federated query during migration.</td>
    </tr>
  </tbody>
</table>

<div class="info">
  <h3>Recommended: Single role that covers assessment + migration</h3>
  <p>For the <strong>assessment</strong>, grant these predefined roles at the <strong>project level</strong>:</p>
  <pre><code># Minimum for a complete assessment
gcloud projects add-iam-policy-binding {gcp_project} \\
  --member="serviceAccount:YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.metadataViewer"

gcloud projects add-iam-policy-binding {gcp_project} \\
  --member="serviceAccount:YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.resourceViewer"

gcloud projects add-iam-policy-binding {gcp_project} \\
  --member="serviceAccount:YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.jobUser"</code></pre>
  <p>For the <strong>migration execution</strong> (Athena connector reading from BigQuery), add:</p>
  <pre><code># Required for the Athena BigQuery Connector (Storage Read API)
gcloud projects add-iam-policy-binding {gcp_project} \\
  --member="serviceAccount:YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.readSessionUser"</code></pre>
</div>

<h3>Permission Scope: Project-Level vs Dataset-Level</h3>

<p>All permissions must be granted at the <strong>project level</strong>, not on individual datasets.
The tool queries project-level <code>INFORMATION_SCHEMA</code> views
(<code>TABLE_STORAGE</code>, <code>JOBS_BY_PROJECT</code>, <code>RESERVATIONS</code>)
which require project-level access. Dataset-level grants will not work for these views.</p>

<!-- ═══════════════ AWS PREREQS ═══════════════ -->

<h2>AWS Prerequisites (for Migration Execution)</h2>

<p>These are needed only when you proceed to the actual migration (terraform + run_migration.py).
The assessment itself runs entirely against GCP.</p>

<div class="card">
  <ol>
    <li><strong>AWS Account</strong> with permissions to create: S3 buckets, Athena workgroups,
      Lambda functions (SAR deploy), Glue databases, IAM policies, Secrets Manager secrets.</li>
    <li><strong>Terraform &ge; 1.5</strong> installed locally.</li>
    <li><strong>AWS CLI v2</strong> configured with credentials for the target account
      (<code>aws sts get-caller-identity</code> should succeed).</li>
    <li><strong>GCP Service Account Key</strong> (JSON) stored in AWS Secrets Manager &mdash;
      the Athena BigQuery Connector uses it to authenticate to GCP.
      See <code>migration/MIGRATION_GUIDE.html</code> Step 1&ndash;2 for detailed instructions.</li>
    <li><strong>Python 3.9+</strong> with <code>boto3</code> for the migration orchestrator
      (<code>pip install -r migration/requirements.txt</code>).</li>
  </ol>
</div>

<!-- ═══════════════ TROUBLESHOOTING ═══════════════ -->

<h2>Troubleshooting</h2>

<p>Permission errors and their fixes are in the <em>Error If Missing</em> column of the table above.
Two cases that trip people up even with roles granted:</p>

<table>
  <thead><tr><th>Symptom</th><th>Cause</th><th>Fix</th></tr></thead>
  <tbody>
    <tr>
      <td>Owner/Editor on the project, still getting <code>403</code> on
        <code>INFORMATION_SCHEMA.TABLE_STORAGE</code></td>
      <td>The basic Owner/Editor roles do <strong>not</strong> include the project-level
        <code>bigquery.tables.get/list</code> permissions these system views require</td>
      <td>Grant <code>roles/bigquery.metadataViewer</code> explicitly, even to project owners</td>
    </tr>
    <tr>
      <td><code>PERMISSION_DENIED</code> on every federated query during migration
        (assessment worked fine)</td>
      <td>The Athena connector needs the Storage Read API, which the assessment never uses</td>
      <td>Grant <code>roles/bigquery.readSessionUser</code> to the service account whose
        key is in Secrets Manager</td>
    </tr>
  </tbody>
</table>

<!-- ═══════════════ RE-RUNNING ═══════════════ -->

<h2>Re-Running the Assessment</h2>

<div class="card">
  <p>If permissions were incomplete during the first run, fix them and re-run:</p>
  <pre><code># Re-run with full permissions (uses ADC — gcloud auth application-default login)
bq-assess --gcp-project {gcp_project} --use-adc --no-cache --include-query-logs

# Or with a service account key
bq-assess --gcp-project {gcp_project} --credentials /path/to/key.json --include-query-logs</code></pre>
  <p>The <code>--no-cache</code> flag forces a fresh scan. The <code>--include-query-logs</code>
  flag enables workload analysis (requires <code>bigquery.jobs.listAll</code>).</p>
  <p>To regenerate reports from a previously-collected bundle (no GCP access needed):</p>
  <pre><code>bq-assess report --bundle bundle/</code></pre>
</div>

<!-- ═══════════════ ABOUT ═══════════════ -->

<h2>About This Assessment</h2>

<div class="card">
  <p>This package was generated by <strong>bq-assess</strong>, a BigQuery &rarr; AWS migration
  assessment tool. It analyzes your BigQuery project metadata (schemas, partitioning, query patterns,
  pricing model) and produces:</p>
  <ul>
    <li>A scored assessment of every entity (tables, views, materialized views, routines)</li>
    <li>Ready-to-deploy Terraform infrastructure for the target AWS environment</li>
    <li>Executable migration scripts with type-correct data loading</li>
    <li>Translated SQL for views and routines on the target engine</li>
  </ul>
  <p><strong>What the tool does NOT access:</strong> table row data. It reads only metadata
  (schemas, INFORMATION_SCHEMA views, view/routine definitions). No customer data
  leaves the GCP project boundary during assessment.</p>
</div>

<div class="footer">
  <p>Generated by <strong>bq-assess</strong> &mdash; BigQuery to AWS Migration Assessment Tool</p>
</div>

</div>
</div>

</body>
</html>"""
