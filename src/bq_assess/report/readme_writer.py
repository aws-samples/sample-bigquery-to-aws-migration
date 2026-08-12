"""Customer-facing README.html generator.

Two modes over one set of shared sections (permissions, AWS prereqs,
troubleshooting, re-running, about — extracted so they cannot drift):

- write_readme: per-project README inside <project>/ (single-project runs).
- write_fleet_readme: ONE top-level README next to SUMMARY.html for
  multi-project runs (2026-08-03 colleague feedback: the customer should read
  a high-level README before SUMMARY.html, not per-project copies). The
  multi-project path skips the per-project READMEs entirely.

Design (2026-08-03 redesign): an orientation page, not a manual. The critical
path (what to open first) is the hero; the folder anatomy is an annotated file
tree; reference material lives in collapsed <details> accordions. Tokens match
the report family (Cloudscape-inspired) so the deliverable reads as one product.
No JavaScript — <details> gives collapsibility offline.
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
    has_query_workload: bool = False,
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
        has_query_workload=has_query_workload,
    ), encoding="utf-8")
    return str(out)


def write_fleet_readme(output_dir: str, project_folders: list[tuple[str, str]]) -> str:
    """Write the ONE top-level README for a multi-project run.

    ``project_folders``: [(gcp_project, folder_name)] for each assessed project.
    Sits next to SUMMARY.html; the per-project READMEs are NOT written in this
    mode (they would be near-identical copies). Returns the file path.
    """
    n = len(project_folders)

    project_rows = "\n".join(
        f'      <a class="proj" href="{folder}/report/{project}-assessment.html">'
        f'<span class="proj__name">{project}</span>'
        f'<span class="proj__folder">{folder}/</span>'
        f'<span class="proj__arrow">&rarr;</span></a>'
        for project, folder in project_folders
    )

    # Annotated file tree — the package as the customer sees it on disk.
    first_folder = project_folders[0][1] if project_folders else "<project>_<date>"
    more = ""
    if n > 1:
        more = "\n" + _tree_row(
            "├─", f"&hellip; {n - 1} more project folder{'s' if n > 2 else ''}", "", dim=True
        )
    tree = f"""
    <div class="tree">
{_tree_row("", "README.html", "this page")}
{_tree_row("├─", "SUMMARY.html", "fleet view — start here")}
{_tree_row("├─", first_folder + "/", "one folder per project — anatomy below", dim=True)}{more}
    </div>"""

    # Anatomy must match what the fleet run ACTUALLY writes (2026-08-04 audit:
    # this tree listed a bundle/ that wasn't shipped and omitted query-workload/).
    inner_tree = f"""
    <div class="tree">
{_tree_row("", "&lt;project&gt;_&lt;date&gt;/", "")}
{_tree_row("├─", "report/", "the assessment — open <code>&lt;project&gt;-assessment.html</code>")}
{_tree_row("├─", "query-workload/", "every production query per table, translated — see <code>INDEX.csv</code>")}
{_tree_row("├─", "terraform/", "infrastructure-as-code for your AWS account")}
{_tree_row("└─", "migration/", "plan.json &middot; run_migration.py &middot; MIGRATION_GUIDE.html &middot; rebuilt_entities.sql &middot; redshift_phase.sql (when present)")}
    </div>"""

    body = f"""
<a class="start" href="SUMMARY.html">
  <span class="start__num">1</span>
  <span class="start__text">
    <span class="start__title">Open SUMMARY.html</span>
    <span class="start__sub">Costs, savings, and recommendations for all {n} project{"s" if n != 1 else ""} &mdash; side by side.</span>
  </span>
  <span class="start__arrow">&rarr;</span>
</a>

<div class="rail">
  <div class="rail__step">
    <span class="rail__num">2</span>
    <p class="rail__title">Drill into each project&rsquo;s report</p>
    <p class="rail__sub">Two tabs per report: <strong>Migration Effort</strong> (moving the data) and
    <strong>Query Complexity</strong> (rewriting the SQL).</p>
  </div>
  <div class="rail__step">
    <span class="rail__num">3</span>
    <p class="rail__title">When ready to migrate</p>
    <p class="rail__sub">Open that project&rsquo;s <code>migration/MIGRATION_GUIDE.html</code> and follow it
    end to end &mdash; terraform deploy, data load, rebuilt entities.</p>
  </div>
</div>

<section class="sect">
  <p class="eyebrow">Assessed projects</p>
  <div class="projects">
{project_rows}
  </div>
</section>

<section class="sect">
  <p class="eyebrow">What&rsquo;s in this package</p>
{tree}
  <p class="eyebrow" style="margin-top:28px">Inside Each Project Folder</p>
{inner_tree}
</section>

<section class="sect">
  <p class="eyebrow">Reference &mdash; open when you need it</p>
{_shared_sections("YOUR_PROJECT_ID")}
</section>"""

    out = Path(output_dir) / "README.html"
    out.write_text(
        _page(meta=f"{n} project{'s' if n != 1 else ''}", body=body),
        encoding="utf-8",
    )
    return str(out)


def _render(
    gcp_project: str,
    has_report: bool,
    has_terraform: bool,
    has_migration: bool,
    has_bundle: bool,
    has_rebuilt_entities: bool,
    has_redshift_phase: bool,
    has_query_workload: bool = False,
) -> str:
    report_file = f"{gcp_project}-assessment.html"

    # Annotated file tree — only the folders this run actually produced.
    rows = []
    if has_report:
        rows.append(("report/", f"the assessment — open <code>{report_file}</code>"))
    if has_query_workload:
        rows.append((
            "query-workload/",
            "every production query per table, translated — see <code>INDEX.csv</code>",
        ))
    if has_terraform:
        rows.append(("terraform/", "infrastructure-as-code for your AWS account"))
    if has_migration:
        mig_files = "plan.json &middot; run_migration.py &middot; MIGRATION_GUIDE.html"
        if has_rebuilt_entities:
            mig_files += " &middot; rebuilt_entities.sql"
        if has_redshift_phase:
            mig_files += " &middot; redshift_phase.sql"
        rows.append(("migration/", mig_files))
    if has_bundle:
        rows.append(("bundle/", "re-processable data export (no GCP access needed)"))
    tree_rows = "\n".join(
        _tree_row("└─" if i == len(rows) - 1 else "├─", name, note)
        for i, (name, note) in enumerate(rows)
    )
    tree = f"""
    <div class="tree">
{_tree_row("", "README.html", "this page")}
{tree_rows}
    </div>"""

    # Steps after the hero — contiguous numbering from 2.
    steps = []
    if has_migration:
        steps.append((
            "Review the migration guide",
            (
                "<code>migration/MIGRATION_GUIDE.html</code> &mdash; step-by-step instructions "
                "for deploying infrastructure and running the migration."
            ),
        ))
    if has_terraform:
        steps.append((
            "Deploy the infrastructure",
            "<code>terraform/</code> into your AWS account (the guide walks through it).",
        ))
    if has_migration:
        steps.append((
            "Load your data",
            (
                "<code>migration/run_migration.py</code> &mdash; Phase 1 creates the tables, "
                "Phase 2 loads the data."
            ),
        ))
    if has_rebuilt_entities:
        steps.append((
            "Recreate views &amp; routines",
            "Apply <code>migration/rebuilt_entities.sql</code> after the data load.",
        ))
    if has_redshift_phase:
        steps.append((
            "Run the Redshift phase",
            (
                "<code>migration/redshift_phase.sql</code> for tables placed on "
                "Redshift Managed Storage."
            ),
        ))
    rail = "\n".join(
        f'''  <div class="rail__step">
    <span class="rail__num">{i}</span>
    <p class="rail__title">{title}</p>
    <p class="rail__sub">{sub}</p>
  </div>'''
        for i, (title, sub) in enumerate(steps, 2)
    )

    body = f"""
<a class="start" href="report/{report_file}">
  <span class="start__num">1</span>
  <span class="start__text">
    <span class="start__title">Open your assessment report</span>
    <span class="start__sub"><code>report/{report_file}</code> &mdash; two tabs:
    <strong>Migration Effort</strong> (moving the data) and <strong>Query Complexity</strong>
    (rewriting the SQL).</span>
  </span>
  <span class="start__arrow">&rarr;</span>
</a>

<div class="rail">
{rail}
</div>

<section class="sect">
  <p class="eyebrow">What&rsquo;s in this package</p>
{tree}
</section>

<section class="sect">
  <p class="eyebrow">Reference &mdash; open when you need it</p>
{_shared_sections(gcp_project)}
</section>"""

    return _page(meta=gcp_project, body=body)


# ─── Shared page pieces (single source: the per-project and fleet READMEs
#     must never drift — test-enforced) ─────────────────────────────────


def _tree_row(branch: str, name: str, note: str, dim: bool = False) -> str:
    cls = "tree__row tree__row--dim" if dim else "tree__row"
    branch_html = (
        f'<span class="tree__branch">{branch}</span>' if branch
        else '<span class="tree__branch tree__branch--root"></span>'
    )
    note_html = f'<span class="tree__note">{note}</span>' if note else ""
    return (
        f'      <div class="{cls}">{branch_html}'
        f'<span class="tree__name">{name}</span>{note_html}</div>'
    )


def _perm(role: str, perms: str, what: str, err: str) -> str:
    return f"""
  <div class="perm">
    <div class="perm__head">
      <span class="chip">{role}</span>
      <span class="perm__perms">{perms}</span>
    </div>
    <p class="perm__what">{what}</p>
    <div class="perm__err">{err}</div>
  </div>"""


_SHARED_SECTIONS_TEMPLATE = (
    """
<details class="ref">
  <summary>
    <span class="ref__title">GCP Permissions Required</span>
    <span class="ref__hint">grant these before running &mdash; fixes every 403</span>
  </summary>
  <div class="ref__body">
  <p>The tool reads metadata only (never table contents). Grant these to the principal
  running it &mdash; at the <strong>project level</strong>, not on individual datasets: the
  <code>INFORMATION_SCHEMA</code> views it queries (<code>TABLE_STORAGE</code>,
  <code>JOBS_BY_PROJECT</code>, <code>RESERVATIONS</code>) do not resolve with dataset-level grants.</p>
"""
    + _perm(
        "roles/bigquery.metadataViewer",
        "<code>bigquery.tables.get</code> &middot; <code>bigquery.tables.list</code>",
        "Schemas, column types, partitioning, clustering &mdash; the core scan. Also unlocks "
        "<code>INFORMATION_SCHEMA.TABLE_STORAGE</code> (measured physical size instead of estimates).",
        "403 Access Denied &hellip; ('bigquery.tables.get', 'bigquery.tables.list' permission(s) "
        "at the project level) &hellip; INFORMATION_SCHEMA.TABLE_STORAGE",
    )
    + _perm(
        "roles/bigquery.resourceViewer",
        "<code>bigquery.jobs.listAll</code> &middot; <code>bigquery.reservations.list</code> &middot; "
        "<code>bigquery.capacityCommitments.list</code> &middot; <code>bigquery.reservationAssignments.list</code>",
        "Query logs (<code>JOBS_BY_PROJECT</code>) for workload analysis and pricing-model detection, "
        "plus reservation/commitment details for enterprise capacity cost modelling.",
        "Missing required permission 'bigquery.jobs.listAll' on project &mdash; without it, complexity "
        "confidence drops to LOW and workload/cost analysis are unavailable",
    )
    + _perm(
        "roles/bigquery.jobUser",
        "<code>bigquery.jobs.create</code>",
        "Running the INFORMATION_SCHEMA queries themselves (every query needs a job).",
        "403 Access Denied: User does not have bigquery.jobs.create permission",
    )
    + _perm(
        "roles/bigquery.readSessionUser",
        "<code>bigquery.readsessions.create</code>",
        "Migration execution only (not assessment): the Athena BigQuery Connector reads table "
        "data via the Storage Read API.",
        "PERMISSION_DENIED: Request had insufficient authentication scopes &mdash; on every "
        "federated query during migration",
    )
    + """
  <p class="ref__sub">Copy-paste grants (assessment first, the last one before migration):</p>
  <pre><code># Assessment
gcloud projects add-iam-policy-binding {gcp_project} \\
  --member="serviceAccount:YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.metadataViewer"
gcloud projects add-iam-policy-binding {gcp_project} \\
  --member="serviceAccount:YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.resourceViewer"
gcloud projects add-iam-policy-binding {gcp_project} \\
  --member="serviceAccount:YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.jobUser"

# Migration execution (Athena connector — Storage Read API)
gcloud projects add-iam-policy-binding {gcp_project} \\
  --member="serviceAccount:YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.readSessionUser"</code></pre>
  </div>
</details>

<details class="ref">
  <summary>
    <span class="ref__title">AWS Prerequisites</span>
    <span class="ref__hint">needed for migration execution only</span>
  </summary>
  <div class="ref__body">
  <p>The assessment runs entirely against GCP. When you proceed to the migration:</p>
  <ul class="checks">
    <li><strong>AWS account</strong> with permissions to create S3 buckets, Athena workgroups,
      Lambda functions (SAR deploy), Glue databases, IAM policies, and Secrets Manager secrets</li>
    <li><strong>Terraform &ge; 1.5</strong> and <strong>AWS CLI v2</strong> configured
      (<code>aws sts get-caller-identity</code> should succeed)</li>
    <li><strong>GCP service-account key</strong> (JSON) stored in AWS Secrets Manager &mdash;
      the Athena BigQuery Connector authenticates with it
      (<code>migration/MIGRATION_GUIDE.html</code> steps 1&ndash;2)</li>
    <li><strong>Python 3.9+</strong> with <code>boto3</code>
      (<code>pip install -r migration/requirements.txt</code>)</li>
  </ul>
  </div>
</details>

<details class="ref">
  <summary>
    <span class="ref__title">Troubleshooting</span>
    <span class="ref__hint">two cases that trip people up even with roles granted</span>
  </summary>
  <div class="ref__body">
  <div class="perm">
    <div class="perm__head"><span class="chip chip--warn">Owner/Editor still gets 403</span></div>
    <p class="perm__what">The basic Owner/Editor roles do <strong>not</strong> include the
    project-level <code>bigquery.tables.get/list</code> that
    <code>INFORMATION_SCHEMA.TABLE_STORAGE</code> requires. Grant
    <code>roles/bigquery.metadataViewer</code> explicitly &mdash; even to project owners.</p>
  </div>
  <div class="perm">
    <div class="perm__head"><span class="chip chip--warn">Assessment worked, migration queries fail</span></div>
    <p class="perm__what"><code>PERMISSION_DENIED</code> on every federated query means the Athena
    connector is missing the Storage Read API, which the assessment never uses. Grant
    <code>roles/bigquery.readSessionUser</code> to the service account whose key is in Secrets Manager.</p>
  </div>
  </div>
</details>

<details class="ref">
  <summary>
    <span class="ref__title">Re-Running the Assessment</span>
    <span class="ref__hint">after fixing permissions, or offline from the bundle</span>
  </summary>
  <div class="ref__body">
  <pre><code># Fresh scan with full permissions (ADC: gcloud auth application-default login)
bq-assess --gcp-project {gcp_project} --use-adc --no-cache

# Or with a service-account key
bq-assess --gcp-project {gcp_project} --credentials /path/to/key.json

# Regenerate reports from an exported bundle — no GCP access needed
# (bundles are produced by bq-collect or bq-assess --export-bundle)
bq-assess report --bundle /path/to/bundle</code></pre>
  <p><code>--no-cache</code> forces a fresh scan. Query-log analysis runs automatically
  (requires <code>bigquery.jobs.listAll</code>; opt out with <code>--skip-workload</code>).</p>
  </div>
</details>

<details class="ref">
  <summary>
    <span class="ref__title">About This Assessment</span>
    <span class="ref__hint">what the tool does &mdash; and what it never touches</span>
  </summary>
  <div class="ref__body">
  <p>Generated by <strong>bq-assess</strong>. It analyzes BigQuery project metadata (schemas,
  partitioning, query patterns, pricing model) and produces a scored assessment of every entity,
  ready-to-deploy Terraform, executable migration scripts with type-correct data loading, and
  translated SQL for views and routines.</p>
  <p><strong>What it does NOT access:</strong> table row data. It reads only metadata
  (schemas, INFORMATION_SCHEMA views, view/routine definitions). No customer data leaves the
  GCP project boundary during assessment.</p>
  </div>
</details>
"""
)


def _shared_sections(gcp_project: str) -> str:
    """Permissions / AWS prereqs / troubleshooting / re-running / about."""
    return _SHARED_SECTIONS_TEMPLATE.replace("{gcp_project}", gcp_project)


_CSS = """  <style>
    :root {
      --bg: #f2f8fd; --panel: #ffffff; --ink: #000716; --navy: #0f1b2a;
      --sub: #5f6b7a; --mut: #7d8998; --line: #e9ebed;
      --blue: #0972d3; --blue-soft: #f0f9ff; --orange: #ff9900;
      --amber: #d97706; --amber-soft: #fffce9;
      --font: "Amazon Ember", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --mono: "Source Code Pro", "Fira Code", ui-monospace, SFMono-Regular, Menlo, monospace;
      --r: 12px; --shadow: 0 1px 2px 0 rgba(0,7,22,.05);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; background: var(--bg); color: var(--ink);
      font: 400 15px/1.65 var(--font); -webkit-font-smoothing: antialiased;
    }
    .wrap { max-width: 860px; margin: 0 auto; padding: 0 24px; }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }
    code {
      font: 500 .85em var(--mono); background: rgba(9,114,211,.08);
      color: #0b5cab; padding: .1em .4em; border-radius: 4px;
    }
    pre {
      background: var(--navy); color: #d5dbdb; padding: 16px 20px;
      border-radius: 8px; overflow-x: auto; font: 400 13px/1.7 var(--mono);
      margin: 12px 0;
    }
    pre code { background: none; color: inherit; padding: 0; }

    /* ── Masthead ── */
    .mast { background: var(--navy); padding: 40px 0 44px; }
    .mast__inner { display: flex; align-items: flex-start; gap: 16px; }
    .cube {
      width: 40px; height: 40px; background: var(--orange); border-radius: 8px;
      display: flex; align-items: center; justify-content: center; flex-shrink: 0;
      font: 800 13px/1 var(--font); color: var(--navy); letter-spacing: .5px;
      margin-top: 4px;
    }
    .mast__eyebrow {
      margin: 0; font-size: 11px; font-weight: 700; letter-spacing: 1.6px;
      text-transform: uppercase; color: var(--orange);
    }
    .mast__title {
      margin: 4px 0 0; font-size: 30px; font-weight: 800; letter-spacing: -.02em;
      color: #fff; line-height: 1.15;
    }
    .mast__meta { margin: 6px 0 0; font-size: 13px; color: rgba(255,255,255,.55); }
    .mast__meta strong { color: rgba(255,255,255,.85); font-weight: 600; }

    .content { padding: 0 0 56px; }

    /* ── Hero action card ── */
    .start {
      display: flex; align-items: center; gap: 18px;
      background: var(--panel); border: 1px solid var(--line);
      border-left: 4px solid var(--blue); border-radius: var(--r);
      padding: 20px 24px; margin-top: -22px; position: relative;
      box-shadow: 0 4px 16px rgba(0,7,22,.08); color: inherit;
      transition: box-shadow .15s ease, transform .15s ease;
    }
    .start:hover { text-decoration: none; box-shadow: 0 6px 22px rgba(0,7,22,.12); transform: translateY(-1px); }
    .start__num {
      width: 36px; height: 36px; border-radius: 50%; background: var(--blue);
      color: #fff; font-weight: 800; font-size: 16px; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
    }
    .start__text { min-width: 0; }
    .start__title { display: block; font-size: 17px; font-weight: 700; color: var(--navy); }
    .start__sub { display: block; font-size: 13.5px; color: var(--sub); margin-top: 2px; }
    .start__arrow {
      margin-left: auto; font-size: 22px; color: var(--blue); flex-shrink: 0;
      transition: transform .15s ease;
    }
    .start:hover .start__arrow { transform: translateX(3px); }

    /* ── Steps rail ── */
    .rail { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 12px; }
    .rail__step {
      background: var(--panel); border: 1px solid var(--line); border-radius: var(--r);
      padding: 16px 18px; box-shadow: var(--shadow); position: relative;
    }
    .rail__num {
      width: 26px; height: 26px; border-radius: 50%;
      border: 2px solid var(--blue); color: var(--blue);
      font-weight: 800; font-size: 13px;
      display: inline-flex; align-items: center; justify-content: center;
    }
    .rail__title { margin: 10px 0 2px; font-size: 14.5px; font-weight: 700; color: var(--navy); }
    .rail__sub { margin: 0; font-size: 13px; color: var(--sub); }

    /* ── Sections ── */
    .sect { margin-top: 40px; }
    .eyebrow {
      margin: 0 0 10px; font-size: 11.5px; font-weight: 700;
      letter-spacing: 1.4px; text-transform: uppercase; color: var(--mut);
    }

    /* ── Project links ── */
    .projects { display: flex; flex-direction: column; gap: 8px; }
    .proj {
      display: flex; align-items: baseline; gap: 14px;
      background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
      padding: 13px 18px; box-shadow: var(--shadow); color: inherit;
      transition: border-color .15s ease;
    }
    .proj:hover { text-decoration: none; border-color: var(--blue); }
    .proj__name { font-weight: 700; color: var(--blue); font-size: 14.5px; }
    .proj__folder { font: 400 12px var(--mono); color: var(--mut); }
    .proj__arrow { margin-left: auto; color: var(--blue); }

    /* ── File tree (signature element) ── */
    .tree {
      background: var(--panel); border: 1px solid var(--line); border-radius: var(--r);
      padding: 18px 22px; box-shadow: var(--shadow);
    }
    .tree__row {
      display: flex; align-items: baseline; gap: 10px;
      padding: 4px 0; font-size: 14px;
    }
    .tree__row + .tree__row { border-top: 1px dashed #f0f2f4; }
    .tree__branch { font: 400 13px var(--mono); color: #c3ccd5; width: 22px; flex-shrink: 0; }
    .tree__branch--root { width: 0; }
    .tree__name { font: 600 13.5px var(--mono); color: var(--navy); white-space: nowrap; }
    .tree__alt { color: var(--mut); font-weight: 400; }
    .tree__note { font-size: 12.5px; color: var(--mut); margin-left: auto; text-align: right; }
    .tree__row--dim .tree__name { color: var(--sub); font-weight: 500; }

    /* ── Reference accordions ── */
    .ref {
      background: var(--panel); border: 1px solid var(--line); border-radius: var(--r);
      box-shadow: var(--shadow); margin-bottom: 10px; overflow: hidden;
    }
    .ref summary {
      display: flex; align-items: baseline; gap: 12px; cursor: pointer;
      padding: 15px 20px; list-style: none;
    }
    .ref summary::-webkit-details-marker { display: none; }
    .ref summary::before {
      content: "›"; font-size: 17px; font-weight: 700; color: var(--mut);
      transition: transform .15s ease; align-self: center;
    }
    .ref[open] summary::before { transform: rotate(90deg); color: var(--blue); }
    .ref summary:hover .ref__title { color: var(--blue); }
    .ref__title { font-size: 15px; font-weight: 700; color: var(--navy); }
    .ref__hint { font-size: 12.5px; color: var(--mut); margin-left: auto; text-align: right; }
    .ref__body { padding: 4px 20px 20px; border-top: 1px solid #f0f2f4; }
    .ref__body > p:first-child { margin-top: 14px; }
    .ref__sub { font-size: 13px; color: var(--sub); margin-bottom: 4px; }

    /* ── Permission cards ── */
    .perm { padding: 14px 0; }
    .perm + .perm { border-top: 1px solid #f0f2f4; }
    .perm__head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .chip {
      font: 600 12px var(--mono); color: #0b5cab; background: var(--blue-soft);
      border: 1px solid rgba(9,114,211,.25); border-radius: 100px; padding: 3px 12px;
      white-space: nowrap;
    }
    .chip--warn { color: var(--amber); background: var(--amber-soft); border-color: rgba(217,119,6,.3); }
    .perm__perms { font-size: 12.5px; color: var(--sub); }
    .perm__what { margin: 8px 0 0; font-size: 13.5px; color: var(--ink); }
    .perm__err {
      margin-top: 8px; font: 400 12px/1.6 var(--mono); color: #9a3b3b;
      background: #fff7f7; border-left: 3px solid #e8b4b4;
      padding: 7px 12px; border-radius: 0 6px 6px 0;
    }

    .checks { margin: 12px 0 0; padding-left: 0; list-style: none; }
    .checks li { padding: 6px 0 6px 26px; position: relative; font-size: 13.5px; }
    .checks li::before {
      content: "✓"; position: absolute; left: 2px; color: var(--blue); font-weight: 700;
    }

    .foot {
      margin-top: 48px; padding-top: 18px; border-top: 1px solid var(--line);
      font-size: 12px; color: var(--mut);
    }

    @media (max-width: 640px) {
      .mast__title { font-size: 24px; }
      .tree__note, .ref__hint { display: none; }
      .start { flex-wrap: wrap; }
    }
    @media (prefers-reduced-motion: reduce) {
      * { transition: none !important; }
    }
  </style>"""


def _page(meta: str, body: str) -> str:
    """Full HTML page: masthead + body + footer around the shared CSS."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Read me first — BigQuery Migration Assessment</title>
{_CSS}
</head>
<body>

<div class="mast">
  <div class="wrap">
    <div class="mast__inner">
      <div class="cube">BQ</div>
      <div>
        <p class="mast__eyebrow">BigQuery &rarr; AWS Migration Assessment</p>
        <h1 class="mast__title">Read me first</h1>
        <p class="mast__meta"><strong>{meta}</strong> &middot; how to read this package, and everything you need to run it</p>
      </div>
    </div>
  </div>
</div>

<div class="content">
<div class="wrap">
{body}
<div class="foot">
  Generated by <strong>bq-assess</strong> &mdash; BigQuery to AWS Migration Assessment Tool
</div>
</div>
</div>

</body>
</html>"""
