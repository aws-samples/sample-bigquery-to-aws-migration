"""Generate executable migration scripts (plan.json + run_migration.py).

Produces a migration/ directory containing:
- plan.json: structured per-table migration plan (statements, order, config)
- run_migration.py: Python orchestrator that executes via boto3 Athena
- requirements.txt: dependencies
- MIGRATION_GUIDE.html: step-by-step instructions for running the migration
- rebuilt_entities.sql: translated CREATE VIEW statements + manual-rewrite
  stubs for MVs/UDFs/procedures (only when the Source has rebuilt entities)
"""
from __future__ import annotations

import json
from pathlib import Path

from bq_assess.models import (
    ConversionResult,
    MigrationDML,
    StoragePlacement,
    StorageTarget,
)
from bq_assess.targets.iceberg.identifiers import quote_full_name


def generate_migration_scripts(
    project_dir: str,
    migration_plans: dict[str, MigrationDML],
    connector_name: str | None,
    target_region: str,
    workgroup_name: str | None = None,
    conversion_results: dict[str, ConversionResult] | None = None,
    storage_placements: dict[str, StoragePlacement] | None = None,
    rebuilt_entities: list | None = None,
    translation_results: dict | None = None,
    dataset_id: str | None = None,
) -> str:
    """Write migration/ directory under project_dir. Returns the migration dir path."""
    mig_dir = Path(project_dir) / "migration"
    mig_dir.mkdir(parents=True, exist_ok=True)

    from bq_assess.engine.athena import naming
    # Derive the S3 Tables catalog/namespace the same way terraform does
    # (single source: engine/athena/naming). dataset_id falls back from the
    # workgroup convention for callers that don't pass it.
    ds = dataset_id or (workgroup_name or "bq-migration-default").removeprefix("bq-migration-")
    iceberg_catalog = f"s3tablescatalog/{naming.table_bucket_name(ds)}"
    iceberg_database = naming.namespace_name(ds)

    conversions = conversion_results or {}
    placements = storage_placements or {}
    _write_plan_json(mig_dir, migration_plans, conversions, placements)
    _write_redshift_phase_sql(mig_dir, migration_plans, placements)
    _write_run_migration(
        mig_dir, target_region, workgroup_name or "bq-migration",
        iceberg_catalog=iceberg_catalog, iceberg_database=iceberg_database,
    )
    _write_requirements(mig_dir)
    rebuilt_summary = _write_rebuilt_entities_sql(
        mig_dir, rebuilt_entities or [], translation_results or {},
        migrated_tables=set(migration_plans),
    )
    _write_migration_guide(
        mig_dir,
        migration_plans=migration_plans,
        connector_name=connector_name,
        target_region=target_region,
        workgroup_name=workgroup_name or "bq-migration",
        conversion_results=conversions,
        storage_placements=placements,
        rebuilt_summary=rebuilt_summary,
    )

    return str(mig_dir)


def _write_rebuilt_entities_sql(
    mig_dir: Path, rebuilt_entities: list, translation_results: dict,
    migrated_tables: set | None = None,
) -> dict:
    """Write rebuilt_entities.sql — the Rebuilt-population migration deliverable.

    Tables move data; views/MVs/UDFs/procedures are REBUILT on the target engine.
    plan.json / run_migration.py only cover tables, so without this file the
    rebuilt entities silently fall out of the migration deliverable (they were
    previously only visible in the assessment report's Query Complexity tab).

    Emits per entity:
    - VIEW with a usable translation → runnable CREATE OR REPLACE VIEW
    - MATERIALIZED VIEW → no Athena equivalent; translated SELECT provided as a
      plain CREATE VIEW alternative (commented) + scheduled-CTAS note
    - ROUTINE (SQL) → translated body as a comment (Athena has no CREATE FUNCTION
      for SQL UDFs; inline it or use a Lambda UDF)
    - ROUTINE (JS) / blocked translations → manual-rewrite stub

    Returns a summary dict for the migration guide (counts + flags). Writes the
    file only when rebuilt entities exist; returns zero counts otherwise.
    """
    summary = {"views": 0, "mviews": 0, "routines": 0, "manual": 0, "written": False}
    if not rebuilt_entities:
        return summary

    # The translations carry their dialect (TranslationResult.target_engine) —
    # the execution instructions MUST match it. A previous version hardcoded
    # "run in the Athena workgroup" around Redshift-dialect SQL (2026-08-04
    # audit: every shipped view was unrunnable on the instructed engine).
    target_engine = next(
        (getattr(tr, "target_engine", "redshift")
         for tr in translation_results.values() if tr is not None),
        "redshift",
    )

    def _tr(full_name):
        tr = translation_results.get(full_name)
        if tr is None:
            return None, [], True
        warnings = list(getattr(tr, "warnings", []) or [])
        blocked = any("BLOCKER" in w for w in warnings)
        return getattr(tr, "redshift_sql", "") or "", warnings, blocked

    sections: list[str] = []
    for entity in sorted(rebuilt_entities, key=lambda e: e.full_name):
        etype = entity.entity_type.value
        sql, warnings, blocked = _tr(entity.full_name)
        warn_lines = "".join(f"-- WARNING: {w}\n" for w in warnings)

        # Views whose base tables are NOT in the migration plan can never
        # validate on the target — flag them at the top of the section rather
        # than let the operator discover it at CREATE time (2026-08-04 audit:
        # 6 views depended on datasets absent from the plan).
        missing_deps = []
        if migrated_tables is not None and etype in ("VIEW", "MATERIALIZED_VIEW"):
            rebuilt_names = {e.full_name for e in rebuilt_entities}
            missing_deps = sorted(
                dep for dep in (entity.depends_on or [])
                if dep not in migrated_tables and dep not in rebuilt_names
            )
        dep_lines = "".join(
            f"-- WARNING: depends on {d}, which is NOT in this migration plan — "
            f"this view cannot validate until that table exists on the target.\n"
            for d in missing_deps
        )

        if etype == "VIEW" and sql and not blocked:
            summary["views"] += 1
            sections.append(
                f"-- ── VIEW {entity.full_name} "
                f"{'─' * max(1, 60 - len(entity.full_name))}\n"
                f"{dep_lines}{warn_lines}"
                f"CREATE OR REPLACE VIEW {quote_full_name(entity.full_name)} AS\n{sql.rstrip(';')};\n"
            )
        elif etype == "MATERIALIZED_VIEW":
            summary["mviews"] += 1
            body = f"\n-- CREATE OR REPLACE VIEW {quote_full_name(entity.full_name)} AS\n-- " + \
                "\n-- ".join(sql.rstrip(";").splitlines()) + "\n" if sql and not blocked else "\n"
            if target_engine == "athena":
                mv_note = (
                    "-- Athena cannot CREATE MATERIALIZED VIEW. Two options:\n"
                    "--   a) plain view (recomputes per query) — uncomment below\n"
                    "--   b) scheduled CTAS refresh (EventBridge/Airflow: CREATE TABLE AS + swap)\n"
                )
            else:
                mv_note = (
                    "-- Redshift supports native materialized views: replace the commented\n"
                    "-- CREATE OR REPLACE VIEW below with CREATE MATERIALIZED VIEW ... AUTO\n"
                    "-- REFRESH YES once the base tables are loaded (a plain view also works).\n"
                )
            sections.append(
                f"-- ── MATERIALIZED VIEW {entity.full_name} "
                f"{'─' * max(1, 47 - len(entity.full_name))}\n"
                f"{dep_lines}{mv_note}"
                f"{warn_lines}{body}"
            )
        elif etype == "ROUTINE" and sql and not blocked:
            summary["routines"] += 1
            body = "\n-- ".join(sql.rstrip(";").splitlines())
            if target_engine == "athena":
                routine_note = (
                    "-- Athena has no CREATE FUNCTION for SQL UDFs — inline this translated\n"
                    "-- body at call sites, or wrap it as an Athena Lambda UDF.\n"
                )
            else:
                routine_note = (
                    "-- Wrap this translated body in CREATE FUNCTION ... LANGUAGE SQL on\n"
                    "-- Redshift (scalar SQL UDF), or inline it at call sites.\n"
                )
            sections.append(
                f"-- ── ROUTINE {entity.full_name} "
                f"{'─' * max(1, 57 - len(entity.full_name))}\n"
                f"{routine_note}"
                f"{warn_lines}"
                f"-- {body}\n"
            )
        else:
            summary["manual"] += 1
            reason = "; ".join(w.replace("BLOCKER: ", "") for w in warnings if "BLOCKER" in w) \
                or "no automatic translation available"
            sections.append(
                f"-- ── {etype} {entity.full_name} — MANUAL REWRITE REQUIRED "
                f"{'─' * max(1, 30 - len(entity.full_name))}\n"
                f"-- Reason: {reason}\n"
                f"-- See the assessment report (Query Complexity tab) for the analyzed\n"
                f"-- source SQL and rewrite guidance.\n"
            )

    if target_engine == "athena":
        exec_lines = (
            "-- Dialect: Athena (Trino). Execute statements individually (Athena\n"
            "-- accepts one per call), in the workgroup created by terraform.\n"
        )
    else:
        exec_lines = (
            "-- Dialect: Amazon Redshift Serverless (the recommended Query Engine).\n"
            "-- Execute in Redshift (Query Editor v2 or any SQL client) AFTER the\n"
            "-- external schemas over the migrated Iceberg tables exist — do NOT\n"
            "-- run this file in the Athena workgroup; the SQL is Redshift dialect.\n"
        )
    header = (
        "-- ═══════════════════════════════════════════════════════════════════\n"
        "-- REBUILT ENTITIES — views, materialized views, UDFs, procedures\n"
        "-- ═══════════════════════════════════════════════════════════════════\n"
        "-- These entities are REBUILT on the target engine (no data to move).\n"
        "-- Run AFTER run_migration.py completes: views reference the loaded\n"
        "-- Iceberg tables and fail validation until those exist.\n"
        f"-- Contents: {summary['views']} runnable view(s), {summary['mviews']} materialized\n"
        f"-- view alternative(s), {summary['routines']} routine translation(s),\n"
        f"-- {summary['manual']} manual-rewrite item(s).\n"
        f"{exec_lines}"
        "-- Review every statement before running.\n"
        "-- ═══════════════════════════════════════════════════════════════════\n\n"
    )
    (mig_dir / "rebuilt_entities.sql").write_text(header + "\n".join(sections), encoding="utf-8")
    summary["written"] = True
    return summary


def _write_plan_json(
    mig_dir: Path,
    plans: dict[str, MigrationDML],
    conversion_results: dict[str, ConversionResult],
    storage_placements: dict[str, StoragePlacement],
) -> None:
    plan_data = {
        "tables": []
    }

    for full_name, dml in plans.items():
        conversion = conversion_results.get(full_name)
        ddl = conversion.ddl if conversion and conversion.ddl else None
        placement = storage_placements.get(full_name)
        is_rms = placement is not None and placement.target == StorageTarget.RMS

        entry = {
            "table": full_name,
            # ADR-0005: every table declares its Storage Target. Iceberg entries run
            # phase 1 only (Athena INSERT into the final table). RMS entries treat the
            # Iceberg table as STAGING and add a redshift_phase (run in Redshift,
            # NOT via the Athena orchestrator).
            "storage_target": placement.target.value if placement else "iceberg",
            "ddl": ddl,
            "statements": dml.statements,
            "validation_query": dml.validation_query,
            "estimated_scan_bytes": dml.estimated_scan_bytes,
            "shortcomings": [
                {
                    "category": s.category,
                    "severity": s.severity,
                    "description": s.description,
                    "remediation": s.remediation,
                    "remediation_engine": s.remediation_engine,
                }
                for s in dml.shortcomings
            ],
            "post_optimization": [
                {
                    "step_type": p.step_type,
                    "command": p.command,
                    "engine": p.engine,
                    "reason": p.reason,
                    "priority": p.priority,
                }
                for p in dml.post_optimization
            ],
        }
        if is_rms:
            entry["redshift_phase"] = {
                "ddl": placement.redshift_ddl,
                "statements": placement.redshift_load or [],
                "signals": placement.signals,
                "note": (
                    "Run these in Redshift AFTER the Athena statements above complete. "
                    "The Iceberg table acts as a staging area; validate row counts "
                    "before dropping it."
                ),
            }
        plan_data["tables"].append(entry)

    # No indent: at warehouse scale indent=2 roughly doubles the file (a 50k-table
    # estate produced a 150 MB plan.json). run_migration.py json.loads() it either way;
    # pipe through `python3 -m json.tool` to inspect by eye.
    (mig_dir / "plan.json").write_text(
        json.dumps(plan_data, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_redshift_phase_sql(
    mig_dir: Path,
    plans: dict[str, MigrationDML],
    storage_placements: dict[str, StoragePlacement],
) -> None:
    """Emit redshift_phase.sql for RMS-placed tables (ADR-0005).

    run_migration.py drives Athena only; the Redshift phase runs separately
    (Query Editor v2, psql, or any Redshift client) AFTER the Athena load
    completes. No file is written when every table stays on Iceberg.
    """
    from bq_assess.engine.redshift.storage_placement import ICEBERG_EXTERNAL_SCHEMA

    rms_tables = [
        (name, storage_placements[name])
        for name in plans
        if name in storage_placements
        and storage_placements[name].target == StorageTarget.RMS
    ]
    if not rms_tables:
        return

    rms_datasets = sorted({name.split(".")[0] for name, _ in rms_tables})
    prereq_lines: list[str] = []
    for ds in rms_datasets:
        prereq_lines += [
            f"-- Dataset {ds}:",
            f"--   1. Target schema:  CREATE SCHEMA IF NOT EXISTS {ds};",
            "--   2. Glue resource link in the DEFAULT catalog (Redshift cannot address",
            "--      the s3tablescatalog federated sub-catalog directly):",
            "--        aws glue create-database --catalog-id <account> --database-input '{",
            f'--          "Name": "{ds}_rl",',
            '--          "TargetDatabase": {"CatalogId": "<account>:s3tablescatalog/<table-bucket>",',
            f'--                              "DatabaseName": "{ds}"}}}}\'',
            "--   3. External schema over the link (one per dataset):",
            f"--        CREATE EXTERNAL SCHEMA IF NOT EXISTS {ICEBERG_EXTERNAL_SCHEMA}_{ds} FROM DATA CATALOG",
            f"--        DATABASE '{ds}_rl' IAM_ROLE default REGION '<region>' CATALOG_ID '<account>';",
        ]

    parts: list[str] = [
        "-- ═══════════════════════════════════════════════════════════════════",
        "-- REDSHIFT PHASE (run AFTER run_migration.py completes successfully)",
        "-- ═══════════════════════════════════════════════════════════════════",
        "--",
        "-- These tables are placed on Redshift Managed Storage (RMS) per the",
        "-- assessment's storage-placement recommendation (type fidelity /",
        "-- sub-second SLA). The Iceberg tables created by run_migration.py act",
        "-- as STAGING for them.",
        "--",
        "-- EASIEST PATH: run_migration.py --phase 3 --redshift-workgroup <name>",
        "-- automates every prerequisite below. To run manually instead:",
        "--",
        *prereq_lines,
        "--",
        "-- Lake Formation (grants do NOT propagate through resource links): the",
        "-- Redshift workgroup's default IAM role needs DESCRIBE on each resource",
        "-- link plus SELECT/DESCRIBE on the S3 Tables namespace tables.",
        "-- (Live-verified sequence, 2026-07-30.)",
        "-- Validate row counts before running the DROP TABLE cleanup (via Athena).",
        "",
    ]
    for name, placement in rms_tables:
        parts.append(f"-- ──────────────── {name} ────────────────")
        for signal in placement.signals:
            parts.append(f"-- why: {signal}")
        parts.append("")
        if placement.redshift_ddl:
            parts.append(placement.redshift_ddl)
            parts.append("")
        for stmt in placement.redshift_load or []:
            parts.append(stmt)
            parts.append("")

    (mig_dir / "redshift_phase.sql").write_text("\n".join(parts), encoding="utf-8")


def _write_run_migration(
    mig_dir: Path, target_region: str, workgroup_name: str,
    *, iceberg_catalog: str, iceberg_database: str,
) -> None:
    content = (
        _RUN_MIGRATION_TEMPLATE
        .replace("__REGION__", target_region)
        .replace("__WORKGROUP__", workgroup_name)
        .replace("__ICEBERG_CATALOG__", iceberg_catalog)
        .replace("__ICEBERG_DATABASE__", iceberg_database)
    )
    (mig_dir / "run_migration.py").write_text(content, encoding="utf-8")


def _write_requirements(mig_dir: Path) -> None:
    content = "boto3>=1.34\n"
    (mig_dir / "requirements.txt").write_text(content, encoding="utf-8")


def _write_migration_guide(
    mig_dir: Path,
    migration_plans: dict[str, MigrationDML],
    connector_name: str | None,
    target_region: str,
    workgroup_name: str,
    conversion_results: dict[str, ConversionResult],
    storage_placements: dict[str, StoragePlacement] | None = None,
    rebuilt_summary: dict | None = None,
) -> None:
    total_tables = len(migration_plans)
    total_statements = sum(len(dml.statements) for dml in migration_plans.values())
    total_bytes = sum(dml.estimated_scan_bytes or 0 for dml in migration_plans.values())
    total_gb = total_bytes / (1024**3)
    placements = storage_placements or {}
    rms_names = sorted(
        name for name in migration_plans
        if name in placements and placements[name].target == StorageTarget.RMS
    )

    shortcomings_action = []
    shortcomings_advisory = []
    for full_name, dml in migration_plans.items():
        for s in dml.shortcomings:
            entry = f"<li><strong>{full_name}</strong>: {s.description}"
            if s.remediation:
                entry += f"<br><em>Remediation:</em> {s.remediation}"
            entry += "</li>"
            if s.severity == "action_required":
                shortcomings_action.append(entry)
            else:
                shortcomings_advisory.append(entry)

    action_section = ""
    if shortcomings_action:
        action_section = f"""
    <div class="warning">
      <h3>Action Required ({len(shortcomings_action)} items)</h3>
      <p>These issues are handled in the generated SQL (type casts are emitted automatically),
      but you should review them to confirm accuracy:</p>
      <ul>
        {"".join(shortcomings_action)}
      </ul>
    </div>"""

    advisory_section = ""
    if shortcomings_advisory:
        advisory_section = f"""
    <div class="info">
      <h3>Advisory Notes ({len(shortcomings_advisory)} items)</h3>
      <p>Non-blocking — the migration will succeed, but consider these for production workloads:</p>
      <ul>
        {"".join(shortcomings_advisory)}
      </ul>
    </div>"""

    rms_section = ""
    if rms_names:
        rms_items = "".join(f"<li><code>{n}</code></li>" for n in rms_names)
        rms_section = f"""
<h2>Redshift Phase (RMS-Placed Tables)</h2>

<div class="warning">
  <h3>{len(rms_names)} table(s) placed on Redshift Managed Storage</h3>
  <p>The assessment recommends RMS (not Iceberg) as the final home for these tables —
  for type fidelity (GEOGRAPHY/JSON/INTERVAL survive natively) and/or sub-second SLA
  (result caching, AutoMV, and zone maps are RMS-only):</p>
  <ul>{rms_items}</ul>
  <p>For these tables the Iceberg tables created above act as <strong>staging</strong>.
  The load runs as <strong>Phase 3</strong> of the orchestrator via the redshift-data API:
  <code>python run_migration.py --phase 3 --redshift-workgroup &lt;name&gt; --redshift-database &lt;db&gt;</code>
  (or as part of a plain <code>run_migration.py</code> run when those flags are set).
  Alternatively, run <code>redshift_phase.sql</code> manually in Redshift Serverless (Query Editor v2
  or any SQL client). Either path creates the native tables and loads them via
  <code>INSERT INTO … SELECT</code> from the Iceberg external schema. Validate
  row counts, then drop the staging tables via Athena (commented commands included).</p>
  <p><em>Trade-off:</em> RMS tables are queryable by Redshift Serverless only — they leave the
  multi-engine storage layer. This is intentional per the per-entity recommendation.</p>
</div>"""

    rebuilt = rebuilt_summary or {}
    rebuilt_section = ""
    if rebuilt.get("written"):
        manual_note = ""
        if rebuilt.get("manual"):
            manual_note = f"""
      <p><strong>{rebuilt["manual"]} entit{"y needs" if rebuilt["manual"] == 1 else "ies need"} a manual rewrite</strong>
      (JavaScript UDFs, procedural scripts, or blocked translations) — each has a stub in the file
      naming the reason; the assessment report's Query Complexity tab has the analyzed source SQL
      and rewrite guidance.</p>"""
        mv_note = ""
        if rebuilt.get("mviews"):
            mv_note = f"""
      <p><strong>{rebuilt["mviews"]} materialized view(s)</strong>: Athena cannot
      <code>CREATE MATERIALIZED VIEW</code>. The file provides each one's translated SELECT as a
      plain-view alternative (recomputes per query) and notes the scheduled-CTAS option for
      cases where materialization matters.</p>"""
        rebuilt_section = f"""
<h2>Rebuilt Entities (views, materialized views, UDFs, procedures)</h2>

<div class="info">
  <h3>Run <code>rebuilt_entities.sql</code> after the data load</h3>
  <p>Tables move data; these entities are <strong>rebuilt</strong> on the target engine.
  <code>run_migration.py</code> does not create them — apply <code>rebuilt_entities.sql</code>
  once Phase 2 completes (the views reference the loaded Iceberg tables).</p>
  <ul>
    <li><strong>{rebuilt.get("views", 0)} view(s)</strong> — runnable translated <code>CREATE OR REPLACE VIEW</code> statements</li>
    <li><strong>{rebuilt.get("mviews", 0)} materialized view(s)</strong> — plain-view alternative + refresh options</li>
    <li><strong>{rebuilt.get("routines", 0)} routine(s)</strong> — translated bodies to inline or wrap as Lambda UDFs</li>
    <li><strong>{rebuilt.get("manual", 0)} manual rewrite(s)</strong> — flagged with reasons</li>
  </ul>{mv_note}{manual_note}
  <p>Execute statements one at a time (Athena accepts a single statement per call) and review
  each before running.</p>
</div>"""

    connector_display = connector_name or "bq-connector-&lt;dataset&gt;"

    # Derive database name from migration plan table names (dataset.table → dataset)
    datasets_in_plan = sorted({name.split(".")[0] for name in migration_plans if "." in name})
    glue_db_display = datasets_in_plan[0] if datasets_in_plan else "dataset_name"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Migration Guide — BigQuery to Iceberg via Athena</title>
  <style>
    /* Migration Guide — Cloudscape-inspired design system (matches assessment report) */
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
      --space-xs: 4px;
      --space-sm: 8px;
      --space-md: 16px;
      --space-lg: 24px;
      --space-xl: 32px;
    }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: var(--font-body);
      font-size: 14px;
      line-height: 1.6;
      color: var(--color-text-body);
      background: var(--color-bg-layout);
      margin: 0;
      padding: 0;
      -webkit-font-smoothing: antialiased;
    }}
    .container {{ max-width: 960px; margin: 0 auto; padding: 0 var(--space-lg); }}
    h1, h2, h3 {{ margin-top: 0; font-weight: 700; line-height: 1.25; color: var(--color-text-heading); }}
    h1 {{ font-size: 1.5rem; letter-spacing: -0.01em; }}
    h2 {{ font-size: 1.125rem; margin: var(--space-xl) 0 var(--space-md); letter-spacing: -0.005em; }}
    h3 {{ font-size: 0.9375rem; margin-bottom: var(--space-md); }}
    p {{ margin-top: 0; margin-bottom: var(--space-md); }}
    li {{ margin-bottom: var(--space-sm); }}
    ul, ol {{ padding-left: 1.5rem; margin-top: 0; }}
    a {{ color: var(--color-text-link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{ font-family: var(--font-mono); font-size: 0.8125rem; background: #f4f4f4; padding: 0.15rem 0.4rem; border-radius: 4px; }}
    pre {{
      background: var(--color-bg-header);
      color: #d5dbdb;
      padding: var(--space-md);
      border-radius: var(--radius-sm);
      overflow-x: auto;
      margin: var(--space-md) 0;
      font-size: 0.8125rem;
      font-family: var(--font-mono);
    }}
    pre code {{ background: none; padding: 0; color: inherit; }}

    /* ─── Header (matches assessment report) ─── */
    .header {{ background: var(--color-bg-header); padding: 0; }}
    .header-inner {{ display: flex; align-items: center; padding: var(--space-md) 0; min-height: 56px; }}
    .header-logo {{ display: flex; align-items: center; gap: var(--space-sm); color: var(--color-text-inverse); font-weight: 700; font-size: 0.9375rem; }}
    .header-logo .aws-cube {{
      width: 32px; height: 32px;
      background: var(--color-brand-orange);
      border-radius: 6px;
      display: flex; align-items: center; justify-content: center;
      font-weight: 800; font-size: 11px; color: var(--color-bg-header);
      letter-spacing: 0.5px;
    }}
    .header-meta {{ margin-left: auto; color: rgba(255,255,255,0.6); font-size: 0.8125rem; }}
    .header-meta strong {{ color: rgba(255,255,255,0.9); }}

    .content {{ padding: var(--space-xl) 0; }}

    /* ─── Cards (matches assessment report) ─── */
    .step {{
      background: var(--color-bg-default);
      border: 1px solid var(--color-border-card);
      border-radius: var(--radius-md);
      padding: var(--space-lg);
      margin: var(--space-md) 0;
      box-shadow: var(--shadow-xs);
    }}
    .step-num {{
      display: inline-flex; align-items: center; justify-content: center;
      background: var(--color-text-link); color: #fff;
      width: 28px; height: 28px; border-radius: 50%;
      font-weight: 700; font-size: 0.8125rem; margin-right: var(--space-sm);
    }}
    .warning {{
      background: var(--color-severity-warning-bg);
      border: 1px solid var(--color-severity-warning);
      border-radius: var(--radius-sm);
      padding: var(--space-md);
      margin: var(--space-md) 0;
    }}
    .warning h3 {{ color: var(--color-severity-warning); }}
    .info {{
      background: var(--color-severity-info-bg);
      border: 1px solid var(--color-severity-info);
      border-radius: var(--radius-sm);
      padding: var(--space-md);
      margin: var(--space-md) 0;
    }}
    .info h3 {{ color: var(--color-severity-info); }}

    .subtitle {{ color: var(--color-text-secondary); font-size: 0.9375rem; margin: 0 0 var(--space-lg); }}

    /* ─── Tables (matches assessment report) ─── */
    table {{ width: 100%; border-collapse: collapse; margin: var(--space-md) 0; background: var(--color-bg-default); border-radius: var(--radius-sm); overflow: hidden; box-shadow: var(--shadow-xs); }}
    th, td {{ border-bottom: 1px solid var(--color-border-divider); padding: 0.625rem 0.75rem; text-align: left; font-size: 0.8125rem; }}
    th {{ background: #f4f4f4; font-weight: 600; color: var(--color-text-secondary); text-transform: uppercase; letter-spacing: 0.4px; font-size: 0.6875rem; }}
    tr:last-child td {{ border-bottom: none; }}

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
          <h1 style="color:#fff;margin:0;font-size:1.125rem">Migration Guide</h1>
          <p style="color:rgba(255,255,255,.5);margin:2px 0 0 0;font-size:.75rem">BigQuery &rarr; Apache Iceberg (via Amazon Athena)</p>
        </div>
      </div>
      <div class="header-meta">
        Region: <strong>{target_region}</strong> &middot; Workgroup: <strong>{workgroup_name}</strong>
      </div>
    </div>
  </div>
</div>

<div class="content">
<div class="container">

<p class="subtitle">{total_tables} tables &nbsp;|&nbsp; {total_statements} SQL statements &nbsp;|&nbsp; {total_gb:.1f} GB estimated scan &nbsp;|&nbsp; {target_region}</p>

<h2>Prerequisites</h2>

<div class="step">
  <p><span class="step-num">1</span><strong>GCP Service Account Key</strong></p>
  <ol>
    <li>Go to <strong>GCP Console → IAM &amp; Admin → Service Accounts</strong></li>
    <li>Create a service account (or select existing) with roles:
      <ul>
        <li><code>BigQuery Data Viewer</code> — read access to tables</li>
        <li><code>BigQuery Job User</code> — allows running queries</li>
        <li><code>BigQuery Read Session User</code> — the connector reads via the
        BigQuery Storage Read API (<code>bigquery.readsessions.create</code>);
        without it every federated query fails with PERMISSION_DENIED</li>
      </ul>
    </li>
    <li>Click the service account → <strong>Keys</strong> tab → <strong>Add Key</strong> → <strong>Create new key</strong> → <strong>JSON</strong></li>
    <li>Save the downloaded <code>.json</code> file securely</li>
  </ol>
</div>

<div class="step">
  <p><span class="step-num">2</span><strong>Store Key in AWS Secrets Manager</strong></p>
  <pre><code>aws secretsmanager create-secret \\
  --name gcp-bigquery-sa \\
  --secret-string file://path/to/your-key.json \\
  --region {target_region}</code></pre>
  <p>Copy the returned ARN — you'll need it for the next step.</p>
</div>

<div class="step">
  <p><span class="step-num">3</span><strong>AWS Credentials (Least Privilege)</strong></p>
  <p>Terraform generates a least-privilege IAM policy scoped to exactly the resources this
  migration uses (the workgroup, connector Lambda, target Glue database, and the two S3
  buckets). After <code>terraform apply</code> (Step 4), attach it to the role or user that
  will run <code>run_migration.py</code>:</p>
  <pre><code># Get the policy ARN from terraform
POLICY_ARN=$(terraform -chdir=../terraform output -raw migration_operator_policy_arn)

# Attach to the role you run the migration with
aws iam attach-role-policy --role-name YOUR_MIGRATION_ROLE --policy-arn "$POLICY_ARN"
# (or for an IAM user: aws iam attach-user-policy --user-name YOUR_USER --policy-arn "$POLICY_ARN")</code></pre>
  <p>Do not run the migration with admin credentials — the generated policy is all it needs.</p>
</div>

<h2>Step-by-Step Migration</h2>

<div class="step">
  <p><span class="step-num">4</span><strong>Deploy Infrastructure (Terraform)</strong></p>
  <pre><code>cd ../terraform/

# Create your terraform.tfvars from the example
cp terraform.tfvars.example terraform.tfvars

# Edit terraform.tfvars — fill in:
#   aws_account_id = "your-account-id"
#   gcp_secret_name = "secret NAME from step 2 (not the ARN)"

terraform init
terraform plan     # Review what will be created
terraform apply    # Deploy (creates: S3 buckets, Athena workgroup, BQ connector, Glue DB)</code></pre>

  <p>This provisions:</p>
  <table>
    <tr><th>Resource</th><th>Purpose</th></tr>
    <tr><td>Athena BigQuery Connector (Lambda)</td><td>Federates queries to BigQuery — one connector serves <strong>every dataset</strong> in the GCP project (the name carries the primary dataset only as a label)</td></tr>
    <tr><td>Athena Data Catalog (<code>{connector_display}</code>)</td><td>Routes SQL to the connector Lambda</td></tr>
    <tr><td>Athena Workgroup (<code>{workgroup_name}</code>)</td><td>Engine v3 — required for Iceberg DML</td></tr>
    <tr><td>S3 Tables Table Bucket</td><td>Managed Iceberg storage — automatic compaction, snapshot expiry, and unreferenced-file cleanup (no OPTIMIZE/VACUUM needed). run_migration.py sets the bucket's default storage class to Intelligent-Tiering before creating tables: cost-neutral now (Frequent Access bills at Standard rates); data unaccessed for 30/90 days automatically tiers down ~43%/~81%. Note: any read returns a file to the Frequent tier, and periodic full-table scans will re-heat cold partitions.</td></tr>
    <tr><td>S3 Results Bucket</td><td>Athena query results (auto-expires after 7 days)</td></tr>
    <tr><td>Namespace (<code>{glue_db_display}</code>)</td><td>Athena database inside the s3tablescatalog federated catalog</td></tr>
    <tr><td>Lake Formation grants</td><td>CREATE_TABLE/DESCRIBE per namespace + table permissions for the migration operator — the s3tablescatalog federated catalog is <strong>always</strong> governed by Lake Formation; IAM alone is not enough</td></tr>
  </table>

  <p><strong>Lake Formation prerequisites</strong> (skipping these fails every Phase 1
  CREATE TABLE with <code>Insufficient Lake Formation permission(s)</code>):</p>
  <ul>
    <li>The identity running <code>terraform apply</code> must be able to grant Lake
    Formation permissions. If the account has never used Lake Formation, add that
    identity as a <em>Data lake administrator</em> first (Lake Formation console →
    Administrative roles and tasks, or
    <code>aws lakeformation put-data-lake-settings</code>). Without it the grants fail
    with <code>Insufficient Glue permissions to access database</code>.</li>
    <li>The grants default to the identity that runs terraform. If a <em>different</em>
    role runs <code>run_migration.py</code> (the Step 3 pattern), set
    <code>migration_operator_principal_arn</code> in <code>terraform.tfvars</code> to that
    role's ARN so the Lake Formation grants land on the right principal.</li>
  </ul>
</div>

<div class="step">
  <p><span class="step-num">5</span><strong>Run Migration — Phase 1: Create Target Tables</strong></p>
  <p>Creates all {total_tables} Iceberg tables in Glue (empty, no data yet):</p>
  <pre><code># Install dependencies
pip install -r requirements.txt

# Dry run first — shows DDL without executing
python run_migration.py --phase 1 --dry-run

# Execute Phase 1
python run_migration.py --phase 1</code></pre>
  <p>If a table fails, fix the DDL issue and re-run for just that table:</p>
  <pre><code>python run_migration.py --phase 1 --table dataset.table_name</code></pre>
</div>

<div class="step">
  <p><span class="step-num">6</span><strong>Run Migration — Phase 2: Load Data</strong></p>
  <p>Executes federated <code>INSERT INTO ... SELECT FROM</code> statements that read from BigQuery
  and write to Iceberg:</p>
  <pre><code># Dry run first — review the INSERT statements
python run_migration.py --phase 2 --dry-run

# Execute Phase 2 (this reads from BigQuery — charges apply on GCP)
python run_migration.py --phase 2</code></pre>
  <p>For large tables, data is chunked by date range to avoid timeouts. Monitor progress
  in the terminal output.</p>
</div>

<div class="step">
  <p><span class="step-num">7</span><strong>Run Both Phases Together (if confident)</strong></p>
  <pre><code># Full migration in one command
python run_migration.py

# Or with dry-run to preview everything
python run_migration.py --dry-run</code></pre>
  <p>If Phase 1 has any failures, the script stops before Phase 2 automatically.</p>
</div>

{rms_section}
{rebuilt_section}

<h2>Shortcomings &amp; Known Issues</h2>
{action_section}
{advisory_section}

{"<p>No shortcomings detected — all tables should migrate cleanly.</p>" if not shortcomings_action and not shortcomings_advisory else ""}

<h2>Post-Migration Validation</h2>

<div class="step">
  <p><span class="step-num">8</span><strong>Verify Row Counts</strong></p>
  <p>Run in Athena (workgroup: <code>{workgroup_name}</code>):</p>
  <pre><code>-- Compare row counts between source and target
-- Source (via federated connector):
SELECT COUNT(*) FROM "{connector_display}".dataset.table_name;

-- Target (Iceberg):
SELECT COUNT(*) FROM {glue_db_display}.table_name;</code></pre>
</div>

<div class="step">
  <p><span class="step-num">9</span><strong>Spot-Check Data Quality</strong></p>
  <pre><code>-- Sample comparison
SELECT * FROM {glue_db_display}.table_name LIMIT 10;

-- Check for nulls in required columns
SELECT COUNT(*) FROM {glue_db_display}.table_name
WHERE required_column IS NULL;</code></pre>
</div>

<h2>Troubleshooting</h2>

<table>
  <tr><th>Error</th><th>Cause</th><th>Fix</th></tr>
  <tr><td><code>COLUMN_NOT_FOUND</code></td><td>Schema drift between BQ and DDL</td><td>Re-run <code>bq-assess</code> to regenerate DDL</td></tr>
  <tr><td><code>HIVE_METASTORE_ERROR</code></td><td>Glue database or bucket missing</td><td>Verify <code>terraform apply</code> succeeded</td></tr>
  <tr><td><code>FEDERATION_ERROR</code></td><td>BQ connector Lambda unreachable</td><td>Check Secrets Manager ARN and IAM permissions</td></tr>
  <tr><td><code>ACCESS_DENIED</code></td><td>Missing Athena/S3/Glue permissions</td><td>Add policies from Step 3 to your IAM role</td></tr>
  <tr><td>Query timeout</td><td>Large table chunk exceeds Athena limit</td><td>Reduce <code>--chunk-days</code> in bq-assess config</td></tr>
</table>

<h2>Files in This Directory</h2>

<table>
  <tr><th>File</th><th>Purpose</th></tr>
  <tr><td><code>plan.json</code></td><td>Structured migration plan — DDL + INSERT statements per table, storage target, shortcomings, post-optimization steps, and a per-table <code>validation_query</code> (source-vs-target row counts; the source COUNT federates to BigQuery and bills a scan — run once after the load)</td></tr>
  {"<tr><td><code>redshift_phase.sql</code></td><td>Native CREATE TABLE + INSERT…SELECT for RMS-placed tables — run in Redshift Serverless after run_migration.py</td></tr>" if rms_names else ""}
  {"<tr><td><code>rebuilt_entities.sql</code></td><td>Translated CREATE VIEW statements + MV/UDF/procedure rewrite stubs — run after Phase 2</td></tr>" if rebuilt.get("written") else ""}
  <tr><td><code>run_migration.py</code></td><td>Python orchestrator — executes plan.json via boto3 Athena API</td></tr>
  <tr><td><code>requirements.txt</code></td><td>Python dependencies (<code>pip install -r requirements.txt</code>)</td></tr>
  <tr><td><code>MIGRATION_GUIDE.html</code></td><td>This file</td></tr>
</table>

<div class="footer">
  <p>Generated by <strong>bq-assess</strong> — BigQuery to AWS Migration Assessment Tool</p>
  <p>Region: {target_region} | Workgroup: {workgroup_name} | Connector: {connector_display}</p>
</div>

</div>
</div>

</body>
</html>"""

    (mig_dir / "MIGRATION_GUIDE.html").write_text(html, encoding="utf-8")


_RUN_MIGRATION_TEMPLATE = '''#!/usr/bin/env python3
"""BigQuery to Iceberg migration orchestrator.

Reads plan.json and executes the migration in up to three phases:
  Phase 1: Create Iceberg target tables (DDL)
  Phase 2: Load data from BigQuery via federated INSERT statements

Usage:
    python run_migration.py [--dry-run] [--table TABLE_NAME] [--phase 1|2|all]

Prerequisites:
    1. Run `terraform apply` in ../terraform/ first (provisions connector + workgroup)
    2. AWS credentials configured (boto3 default chain: env vars, ~/.aws/credentials, IAM role)
    3. pip install -r requirements.txt
"""
import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

try:
    import boto3
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install -r requirements.txt")
    sys.exit(1)


REGION = "__REGION__"
WORKGROUP = "__WORKGROUP__"
# S3 Tables federated catalog: unqualified dataset.table names in the DDL/DML
# resolve inside this catalog via Athena's QueryExecutionContext.
ICEBERG_CATALOG = "__ICEBERG_CATALOG__"
ICEBERG_DATABASE = "__ICEBERG_DATABASE__"
POLL_INTERVAL = 5
# Phase-specific wait ceilings (doc-anchored, verified 2026-07-22):
# - Athena kills DML at the 30-min default quota (adjustable to 240 min via Service
#   Quotas) — waiting longer than the quota is pointless, Athena cancels first.
# - Redshift Data API statements may run up to 24 h — a shorter client wait would
#   declare a still-running load failed and invite duplicate re-submission, so on
#   timeout we RE-CHECK status instead of blindly cancelling.
ATHENA_MAX_WAIT_SECONDS = 35 * 60      # DML quota (30 min default) + grace
REDSHIFT_MAX_WAIT_SECONDS = 24 * 3600  # Data API server-side maximum
TERRAFORM_DIR = Path(__file__).parent.parent / "terraform"


def load_plan():
    plan_path = Path(__file__).parent / "plan.json"
    if not plan_path.exists():
        print(f"ERROR: {plan_path} not found")
        sys.exit(1)
    return json.loads(plan_path.read_text(encoding="utf-8"))


def _terraform_output(name):
    import subprocess
    try:
        result = subprocess.run(
            ["terraform", "output", "-raw", name],
            cwd=str(TERRAFORM_DIR), capture_output=True, text=True, timeout=30,
        )
        value = result.stdout.strip()
        # `terraform output -raw` exits 0 even with no state, printing a
        # multi-line ANSI "No outputs found" warning to stdout (live-verified
        # 2026-07-30) — a real output value is a single token with no escapes.
        if result.returncode == 0 and value and "\\n" not in value and "\\x1b" not in value:
            return value
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def resolve_table_bucket(cli_value=None, dry_run=False):
    """Resolve the S3 Tables table bucket name: --table-bucket flag, or terraform output."""
    value = cli_value or _terraform_output("table_bucket_name")
    if value:
        return value
    if dry_run:
        # Previewing before `terraform apply` is a legitimate flow — the
        # bucket name is display-only in a dry run, nothing is submitted.
        return "<table-bucket: run terraform apply>"
    print("ERROR: Could not resolve the S3 Tables table bucket.")
    print("  Either run `terraform apply` in ../terraform/ first, or pass --table-bucket <name>.")
    sys.exit(1)


def ensure_intelligent_tiering(table_bucket_arn, region, dry_run=False):
    """Set the table bucket's default storage class to INTELLIGENT_TIERING.

    Must run BEFORE Phase 1: a table's storage class is fixed at creation and
    cannot be changed afterward. INT's Frequent Access tier bills at Standard
    rates, so this is cost-neutral now; data unaccessed 30/90 days tiers down
    automatically (~43%/~81% cheaper). Requires botocore with the
    PutTableBucketStorageClass operation (2026+); on older SDKs we warn and
    continue — tables then land on S3 Standard permanently.
    """
    if dry_run:
        print("  [DRY RUN] put-table-bucket-storage-class INTELLIGENT_TIERING")
        return
    try:
        s3tables = boto3.client("s3tables", region_name=region)
        s3tables.put_table_bucket_storage_class(
            tableBucketARN=table_bucket_arn,
            storageClassConfiguration={"storageClass": "INTELLIGENT_TIERING"},
        )
        print("  Table bucket default storage class: INTELLIGENT_TIERING")
    except Exception as e:  # noqa: BLE001 — never block the migration on tiering
        print(f"  WARNING: could not set INTELLIGENT_TIERING default ({e}).")
        print("  Tables created now will stay on S3 Standard for their lifetime.")
        print("  Fix: upgrade boto3/awscli and run:")
        print(f"    aws s3tables put-table-bucket-storage-class --table-bucket-arn {table_bucket_arn} \\\\")
        print("      --storage-class-configuration storageClass=INTELLIGENT_TIERING")


def wait_for_query(client, execution_id, max_wait_seconds=ATHENA_MAX_WAIT_SECONDS):
    """Poll with exponential backoff (0.5s -> POLL_INTERVAL cap). On timeout,
    cancel the query and report its ID so it never spins forever."""
    waited = 0.0
    delay = 0.5
    while True:
        resp = client.get_query_execution(QueryExecutionId=execution_id)
        state = resp["QueryExecution"]["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return resp
        if waited >= max_wait_seconds:
            print(f"  TIMEOUT after {int(waited)}s — cancelling {execution_id}")
            try:
                client.stop_query_execution(QueryExecutionId=execution_id)
            except Exception:
                pass
            resp["QueryExecution"]["Status"]["State"] = "TIMED_OUT"
            return resp
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, POLL_INTERVAL)


def split_statements(sql):
    """Split a plan entry into individually executable statements.

    Athena's StartQueryExecution accepts exactly ONE statement — chunked plan
    entries carry a DELETE + INSERT pair, so submit them separately. Comment
    lines are stripped first: a comment-only entry yields nothing, and comments
    around/after the final ';' would otherwise be rejected as a second statement.
    """
    body = "\\n".join(
        line for line in sql.split("\\n") if not line.strip().startswith("--")
    )
    return [part.strip() for part in body.split(";") if part.strip()]


def execute_statement(client, sql, dry_run=False, workgroup=None):
    parts = split_statements(sql)
    if not parts:
        return True  # comment-only entry (headers, chunk-window notes)
    for part in parts:
        if not _execute_one(client, part, dry_run, workgroup):
            return False
    return True


_DATE_LITERAL = re.compile(r"DATE '(\\d{4}-\\d{2}-\\d{2})'")


def _fetch_scalar_row(client, sql, workgroup=None):
    """Run a query and return its first data row as a tuple of strings (or None)."""
    resp = client.start_query_execution(
        QueryString=sql, WorkGroup=workgroup or WORKGROUP,
        QueryExecutionContext={"Catalog": ICEBERG_CATALOG, "Database": ICEBERG_DATABASE},
    )
    result = wait_for_query(client, resp["QueryExecutionId"])
    if result["QueryExecution"]["Status"]["State"] != "SUCCEEDED":
        return None
    rows = client.get_query_results(
        QueryExecutionId=resp["QueryExecutionId"]
    )["ResultSet"]["Rows"]
    if len(rows) < 2:
        return None
    return tuple(c.get("VarCharValue") for c in rows[1]["Data"])


def rechunk_statements(client, statements, dry_run=False, workgroup=None):
    """Regenerate chunk windows from the SOURCE's actual data range.

    plan.json bakes windows from table metadata dates (last_modified → now),
    which misses historical data entirely — e.g. a table last touched in April
    holding January–April rows gets April–July windows and loads 0 rows
    (live-verified 2026-07-30). The plan ships a STEP 0 discovery query for
    exactly this; run it, then rewrite the DELETE/INSERT windows to cover
    [MIN, MAX] of the real data. Non-chunked entries pass through untouched.
    """
    discovery = next((s for s in statements if "AS min_val" in s), None)
    chunk_template = next(
        (s for s in statements if "DELETE FROM" in s and "INSERT INTO" in s), None
    )
    if not discovery or not chunk_template:
        return statements
    baked_dates = _DATE_LITERAL.findall(chunk_template)
    if len(baked_dates) < 2:
        return statements

    if dry_run:
        print("  [DRY RUN] would run STEP 0 discovery and rechunk windows to the actual range")
        return statements

    disc_sql = next(iter(split_statements(discovery)), None)
    if not disc_sql:
        return statements
    row = _fetch_scalar_row(client, disc_sql, workgroup)
    if not row or not row[0] or not row[1]:
        print("  Source has no rows in the partition column — nothing to load.")
        return []

    min_date = date.fromisoformat(str(row[0])[:10])
    max_date = date.fromisoformat(str(row[1])[:10])
    window_days = max(
        (date.fromisoformat(baked_dates[1]) - date.fromisoformat(baked_dates[0])).days, 1
    )
    print(f"  STEP 0: actual data range {min_date} .. {max_date} (windows of {window_days} days)")

    rewritten = []
    current = min_date
    end_exclusive = max_date + timedelta(days=1)
    while current < end_exclusive:
        window_end = min(current + timedelta(days=window_days), end_exclusive)
        # Positional rewrite: each DELETE/INSERT pair carries start,end,start,end.
        it = iter([str(current), str(window_end), str(current), str(window_end)])
        rewritten.append(_DATE_LITERAL.sub(lambda m: f"DATE '{next(it)}'", chunk_template))
        current = window_end
    # Preserve the NULL-partition chunk (rows in BigQuery's __NULL__ partition
    # match no date window) — the rebuild above regenerates date windows only.
    rewritten.extend(
        s for s in statements
        if "IS NULL" in s and "DELETE FROM" in s and "INSERT INTO" in s
    )
    return rewritten


def _execute_one(client, sql, dry_run=False, workgroup=None):
    if dry_run:
        preview = sql[:120].replace("\\n", " ")
        print(f"  [DRY RUN] {preview}...")
        return True

    try:
        resp = client.start_query_execution(
            QueryString=sql,
            WorkGroup=workgroup or WORKGROUP,
            # Target catalog/database context: DDL/DML says dataset.table; the
            # s3tablescatalog context routes it into the table bucket. Absolute
            # 3-part source refs ("connector".dataset.table) are unaffected.
            QueryExecutionContext={
                "Catalog": ICEBERG_CATALOG,
                "Database": ICEBERG_DATABASE,
            },
        )
        execution_id = resp["QueryExecutionId"]
        result = wait_for_query(client, execution_id)
        state = result["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            return True
        reason = result["QueryExecution"]["Status"].get("StateChangeReason", "unknown")
        print(f"  {state}: {reason}")
        # Athena cancels DML at the 30-min default quota (CANCELLED, not FAILED).
        # Cancelled queries still bill for data scanned — repeated retries burn
        # money with zero progress, so name the real fix.
        if state in ("CANCELLED", "TIMED_OUT") and ("exceeded" in reason.lower() or "timeout" in reason.lower() or reason == "unknown"):
            print("  NOTE: this looks like the Athena DML timeout (30 min default).")
            print("  Request a 'DML query timeout' increase in Service Quotas (max 240 min)")
            print("  or reduce --chunk-days so each INSERT scans less data.")
        return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


def run_phase_1(client, tables, table_bucket, dry_run=False, workgroup=None):
    """Phase 1: Create all Iceberg target tables (DDL) in the S3 table bucket."""
    ddl_tables = [t for t in tables if t.get("ddl")]
    if not ddl_tables:
        print("  No DDL statements found — skipping Phase 1.")
        return 0, 0

    print(f"Creating {len(ddl_tables)} Iceberg target tables...")
    print(f"Table bucket: {table_bucket} (catalog {ICEBERG_CATALOG})\\n")
    succeeded = 0
    failed = 0

    for table_entry in ddl_tables:
        table_name = table_entry["table"]
        # IF NOT EXISTS makes re-runs idempotent: without it, a second pass
        # fails every table with "Iceberg table to be created already exists"
        # (live-verified 2026-07-30). Loaded data is never touched.
        ddl = table_entry["ddl"].replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
        first_line = ddl.split("\\n")[0][:80]
        print(f"  CREATE: {table_name}")
        print(f"    {first_line}")

        if not execute_statement(client, ddl, dry_run, workgroup=workgroup):
            print(f"    FAILED to create {table_name}")
            failed += 1
        else:
            succeeded += 1

    print(f"\\n  Phase 1 results: {succeeded} created, {failed} failed")
    return succeeded, failed


def run_phase_2(client, tables, dry_run=False, workgroup=None):
    """Phase 2: Load data via federated INSERT statements.

    Template statements ({{placeholders}} an operator must fill in) are
    SKIPPED as manual follow-ups — they cannot be submitted as-is. Tables
    with manual-remediation shortcomings are still ATTEMPTED (the flags are
    conservative; many such loads succeed live) — but when the attempt fails,
    the table is reported as a manual follow-up with its remediation instead
    of a hard failure, so exit codes reflect only unexpected breakage.
    """
    succeeded = 0
    failed = 0
    manual = []

    for table_entry in tables:
        table_name = table_entry["table"]
        statements = table_entry["statements"]
        if not statements:
            continue

        total = len(statements)
        print(f"\\n{'=' * 60}")
        print(f"Table: {table_name} ({total} statements)")
        print(f"{'=' * 60}")

        if any("{{" in stmt for stmt in statements):
            print("  SKIPPED - TEMPLATE: statements contain {{placeholders}} that need")
            print("  operator substitution (see the comments in plan.json for this table).")
            manual.append(table_name)
            continue

        manual_remediations = [
            s for s in table_entry.get("shortcomings", [])
            if s["severity"] == "action_required" and s.get("remediation_engine") == "manual"
        ]
        if table_entry.get("shortcomings"):
            action_required = [s for s in table_entry["shortcomings"] if s["severity"] == "action_required"]
            if action_required:
                print("  WARNING - ACTION REQUIRED:")
                for s in action_required:
                    print(f"    - {s['category']}: {s['description']}")
                    print(f"      Remediation: {s['remediation']}")

        # Chunked tables: replace metadata-derived windows with the source's
        # ACTUAL data range (STEP 0) so historical rows are not missed.
        statements = rechunk_statements(client, statements, dry_run, workgroup)
        if not statements:
            print(f"  Done: {table_name} (source empty — nothing to load)")
            succeeded += 1
            continue
        total = len(statements)

        table_ok = True
        for i, stmt in enumerate(statements, 1):
            first_line = next(
                (line for line in stmt.split("\\n") if line.strip() and not line.strip().startswith("--")),
                stmt[:80]
            )
            print(f"  [{i}/{total}] {first_line[:80]}")

            if not execute_statement(client, stmt, dry_run, workgroup=workgroup):
                print(f"  FAILED at statement {i} - stopping this table.")
                print(f"    Re-run with: python run_migration.py --table '{table_name}' --phase 2")
                table_ok = False
                break

        if table_ok:
            print(f"  Done: {table_name}")
            succeeded += 1
        elif manual_remediations:
            # The plan predicted this load needs manual remediation and the
            # attempt confirmed it — a known limitation, not unexpected breakage.
            print("  This failure was PREDICTED by the plan — follow the remediation above.")
            manual.append(table_name)
        else:
            failed += 1

    if manual:
        print(f"\\n  {len(manual)} table(s) need manual follow-up (not counted as failures):")
        for name in manual:
            print(f"    - {name}")

    return succeeded, failed


def run_phase_3(tables, region, dry_run=False, redshift_workgroup=None, database=None,
                table_bucket=None):
    """Phase 3: Load RMS-placed tables into Redshift via the redshift-data API.

    Executes each table's redshift_phase DDL + statements from plan.json against
    a Redshift Serverless workgroup. Skips cleanly when no RMS tables exist or
    when --redshift-workgroup was not provided (falls back to the manual
    redshift_phase.sql path).
    """
    rms_entries = [t for t in tables if t.get("storage_target") == "rms" and t.get("redshift_phase")]
    if not rms_entries:
        return 0, 0
    if not redshift_workgroup:
        print(f"\\n{len(rms_entries)} RMS table(s) present but --redshift-workgroup not set.")
        print("Run redshift_phase.sql manually, or re-run with:")
        print("  --phase 3 --redshift-workgroup <name> --redshift-database <db>")
        return 0, 0

    rs = boto3.client("redshift-data", region_name=region)
    succeeded = 0
    failed = 0

    def _exec_redshift(sql):
        if dry_run:
            first = sql.strip().splitlines()[0][:100]
            print(f"  [DRY RUN] {first}...")
            return True
        resp = rs.execute_statement(
            WorkgroupName=redshift_workgroup, Database=database or "dev", Sql=sql,
        )
        stmt_id = resp["Id"]
        waited = 0.0
        delay = 0.5
        while True:
            desc = rs.describe_statement(Id=stmt_id)
            status = desc["Status"]
            # Full enum: SUBMITTED | PICKED | STARTED | FINISHED | ABORTED | FAILED.
            # Only the three terminal states end the loop — never treat the
            # intermediate ones as errors.
            if status == "FINISHED":
                return True
            if status in ("FAILED", "ABORTED"):
                print(f"  FAILED: {desc.get('Error', 'unknown')}")
                return False
            if waited >= REDSHIFT_MAX_WAIT_SECONDS:
                # Data API statements can legitimately run up to 24h. Never cancel
                # here — the statement may be mid-write; report the ID so the
                # operator can check `aws redshift-data describe-statement` and
                # decide. Cancelling + retrying would duplicate loaded rows.
                print(f"  Still running after {int(waited)}s — NOT cancelling.")
                print(f"  Check later: aws redshift-data describe-statement --id {stmt_id}")
                return False
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, POLL_INTERVAL)

    # Prerequisites, live-verified against S3 Tables 2026-07-30. For each
    # source dataset with an RMS table:
    #   1. a TARGET schema in Redshift (the CREATE TABLE is dataset.table)
    #   2. a Glue RESOURCE LINK in the DEFAULT catalog pointing at the S3 Tables
    #      namespace — Redshift's external-schema machinery cannot address the
    #      s3tablescatalog federated sub-catalog directly (EntityNotFound), it
    #      resolves databases only through the default catalog
    #   3. an EXTERNAL SCHEMA over that resource link (one per dataset — an
    #      external schema maps exactly one Glue database)
    # LF note: grants do NOT propagate through resource links — the Redshift
    # role needs DESCRIBE on the link itself PLUS SELECT/DESCRIBE on the target
    # namespace tables (see docs: querying-s3Tables.html, resource-links-about).
    datasets = sorted({t["table"].split(".")[0] for t in rms_entries})
    print(f"\\nPreparing Redshift for {len(datasets)} dataset(s): {', '.join(datasets)}")

    table_bucket = table_bucket or _terraform_output("table_bucket_name")
    account_id = None
    try:
        sts = boto3.client("sts", region_name=region)
        account_id = sts.get_caller_identity()["Account"]
    except Exception:
        pass
    glue = boto3.client("glue", region_name=region)

    for ds in datasets:
        # 1. Target schema (idempotent)
        if not _exec_redshift(f"CREATE SCHEMA IF NOT EXISTS {ds};"):
            print(f"ERROR: could not create target schema '{ds}' — aborting Phase 3.")
            return 0, len(rms_entries)
        # 2. Resource link in the default Glue catalog (idempotent)
        if not dry_run and table_bucket and account_id:
            try:
                glue.create_database(
                    CatalogId=account_id,
                    DatabaseInput={
                        "Name": f"{ds}_rl",
                        "TargetDatabase": {
                            "CatalogId": f"{account_id}:s3tablescatalog/{table_bucket}",
                            "DatabaseName": ds,
                        },
                    },
                )
                print(f"  Resource link created: {ds}_rl -> s3tablescatalog/{table_bucket}/{ds}")
            except glue.exceptions.AlreadyExistsException:
                pass
            except Exception as e:
                print(f"  WARNING: could not create resource link {ds}_rl: {e}")
                print(f"  Create it manually (aws glue create-database with TargetDatabase),")
                print(f"  then re-run --phase 3.")
        # 3. External schema over the resource link (idempotent)
        ext_sql = (
            f"CREATE EXTERNAL SCHEMA IF NOT EXISTS iceberg_{ds} FROM DATA CATALOG "
            f"DATABASE '{ds}_rl' IAM_ROLE default REGION '{region}'"
            + (f" CATALOG_ID '{account_id}';" if account_id else ";")
        )
        if not _exec_redshift(ext_sql):
            print(f"ERROR: could not create external schema iceberg_{ds} — aborting Phase 3.")
            print("Check the workgroup's default IAM role (Glue/S3 Tables read) and the")
            print("Lake Formation grants (DESCRIBE on the resource link, SELECT on the")
            print("namespace tables), then re-run --phase 3.")
            return 0, len(rms_entries)

    for entry in rms_entries:
        name = entry["table"]
        phase = entry["redshift_phase"]
        print(f"\\nRMS table: {name}")
        table_ok = True
        # Idempotent re-runs, mirroring Phases 1/2: IF NOT EXISTS on the DDL
        # (Redshift errors on an existing relation) and TRUNCATE before the
        # INSERT so a retry never doubles the loaded rows.
        ddl = phase.get("ddl")
        if ddl:
            ddl = ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
        statements = ([ddl] if ddl else []) + [f"TRUNCATE TABLE {name};"] + [
            s for s in phase.get("statements", []) if not s.strip().startswith("--")
        ]
        for sql in statements:
            first = sql.strip().splitlines()[0][:80]
            print(f"  {first}")
            if not _exec_redshift(sql):
                print(f"  Stopping {name} — staging Iceberg table left intact for retry.")
                table_ok = False
                break
        if table_ok:
            print(f"  Done: {name} (validate row counts, then drop the staging table via Athena)")
            succeeded += 1
        else:
            failed += 1

    return succeeded, failed


def main():
    parser = argparse.ArgumentParser(description="Execute BigQuery to Iceberg migration")
    parser.add_argument("--dry-run", action="store_true", help="Print statements without executing")
    parser.add_argument("--table", help="Migrate only this table (full_name, e.g. dataset.table)")
    parser.add_argument("--phase", choices=["1", "2", "3", "all"], default="all",
                        help="Run only Phase 1 (DDL), Phase 2 (data load), Phase 3 (RMS load via redshift-data), or all (default)")
    parser.add_argument("--region", default=REGION, help="AWS region")
    parser.add_argument("--workgroup", default=WORKGROUP, help="Athena workgroup")
    parser.add_argument("--redshift-workgroup", default=None,
                        help="Redshift Serverless workgroup for Phase 3 (RMS tables). Omit to run redshift_phase.sql manually.")
    parser.add_argument("--redshift-database", default="dev",
                        help="Redshift database for Phase 3 (default: dev)")
    parser.add_argument("--table-bucket",
                        help="S3 Tables table bucket name (default: read from terraform output)")
    parser.add_argument("--skip-tiering", action="store_true",
                        help="Skip setting the INTELLIGENT_TIERING bucket default before Phase 1")
    args = parser.parse_args()

    plan = load_plan()
    tables = plan["tables"]

    if args.table:
        tables = [t for t in tables if t["table"] == args.table]
        if not tables:
            print(f"ERROR: table '{args.table}' not found in plan.json")
            available = [t["table"] for t in plan["tables"]]
            print("Available: " + ", ".join(available[:10]))
            sys.exit(1)

    print(f"Migration plan: {len(tables)} tables")
    print(f"Region: {args.region} | Workgroup: {args.workgroup}")
    if args.dry_run:
        print("MODE: DRY RUN (no queries will be executed)")
    print()

    client = boto3.client("athena", region_name=args.region)

    total_succeeded = 0
    total_failed = 0

    # Phase 1: Create target tables
    if args.phase in ("1", "all"):
        table_bucket = resolve_table_bucket(args.table_bucket, dry_run=args.dry_run)
        print(f"{'=' * 60}")
        print("PHASE 1: Create Iceberg Target Tables")
        print(f"{'=' * 60}\\n")
        if not args.skip_tiering:
            bucket_arn = _terraform_output("table_bucket_arn")
            if bucket_arn:
                ensure_intelligent_tiering(bucket_arn, args.region, dry_run=args.dry_run)
            else:
                print("  WARNING: table_bucket_arn terraform output unavailable — skipping tiering setup.")
        p1_ok, p1_fail = run_phase_1(client, tables, table_bucket, dry_run=args.dry_run, workgroup=args.workgroup)
        total_succeeded += p1_ok
        total_failed += p1_fail

        if p1_fail > 0 and args.phase == "all":
            print("\\nERROR: Phase 1 had failures — cannot proceed to Phase 2.")
            print("Fix the DDL issues, then re-run with --phase 1 for failed tables,")
            print("then --phase 2 to load data.")
            sys.exit(1)

    # Phase 2: Load data
    if args.phase in ("2", "all"):
        print(f"\\n{'=' * 60}")
        print("PHASE 2: Load Data from BigQuery")
        print(f"{'=' * 60}")
        p2_ok, p2_fail = run_phase_2(client, tables, dry_run=args.dry_run, workgroup=args.workgroup)
        total_succeeded += p2_ok
        total_failed += p2_fail

        if p2_fail > 0 and args.phase == "all":
            print("\\nERROR: Phase 2 had failures — not proceeding to Phase 3 (RMS load).")
            print("Fix and re-run with --table <name>, then --phase 3 for RMS tables.")
            sys.exit(1)

    # Phase 3: RMS tables — Redshift native load (gated on Phases 1+2 succeeding)
    if args.phase in ("3", "all"):
        rms_present = any(t.get("storage_target") == "rms" for t in tables)
        if rms_present:
            print(f"\\n{'=' * 60}")
            print("PHASE 3: Load RMS Tables into Redshift (from Iceberg staging)")
            print(f"{'=' * 60}")
            p3_ok, p3_fail = run_phase_3(
                tables, args.region, dry_run=args.dry_run,
                redshift_workgroup=args.redshift_workgroup,
                database=args.redshift_database,
                table_bucket=args.table_bucket,
            )
            total_succeeded += p3_ok
            total_failed += p3_fail

    print(f"\\n{'=' * 60}")
    print(f"RESULTS: {total_succeeded} succeeded, {total_failed} failed")
    print(f"{'=' * 60}")

    if total_failed > 0:
        print("\\nRe-run failed tables individually with --table <name>")
        sys.exit(1)


if __name__ == "__main__":
    main()
'''
