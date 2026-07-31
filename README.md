# sample-bigquery-to-aws-migration

Assess migrating a **Google BigQuery** warehouse to an **AWS lakehouse** — data in
**Amazon S3 Tables (Apache Iceberg)**, queried by **Amazon Redshift Serverless** or
**Amazon Athena**. The `bq-assess` CLI scans BigQuery metadata (read-only, never table
data), scores every table on two independent axes — **Migration Effort** (moving the
data) and **Query Complexity** (keeping the SQL running) — and generates Iceberg DDL,
load guidance, an engine recommendation, and a directional BigQuery-vs-AWS cost
comparison as HTML + JSON reports.

It **assesses; it does not execute the migration** — and it needs no AWS account to run.

> **Beta.** Cost figures are directional estimates, not a pricing quote, and are labelled
> by confidence in the report. Assessments are best reviewed with your AWS specialist
> team before they inform a decision.

## Two Ways to Run

**1. Collect, then report — recommended when working with an AWS team**

Run the lightweight collector where your BigQuery credentials live. It writes a
plain-JSON, checksummed bundle you can inspect before sharing; your AWS team generates
the report from it, fully offline.

➡️ **[Collect and Share — customer guide](docs/COLLECT.md)** — the three commands to run,
what's in the bundle, and how to review it.

```bash
bq-collect --gcp-project my-project --use-adc --output bundle-out/
# then, on the analyst side:
bq-assess report --bundle bundle-out/ --output reports/
```

**2. Full assessment — scan and report in one step**

Run the whole pipeline yourself, in the environment that has BigQuery access. Given the
beta caveat above, we suggest looping in your AWS specialist team to interpret the
output.

## Prerequisites

- Python 3.9+
- [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed
- GCP authentication configured:

  ```bash
  gcloud auth application-default login
  ```

- IAM role on the target project: `roles/bigquery.metadataViewer`
- For query log analysis (optional, higher confidence): `roles/bigquery.resourceViewer`

## Full Assessment — Claude Code

The guided path. The skill handles setup, execution, and report interpretation.

```
/plugin marketplace add aws-samples/sample-bigquery-to-aws-migration
/plugin install bq-assess@sample-bigquery-to-aws-migration
```

Then ask:

> "Assess BigQuery migration for project my-project"

## Full Assessment — CLI

Install directly:

```bash
pip3 install "git+https://github.com/aws-samples/sample-bigquery-to-aws-migration.git"
```

Then:

```bash
bq-assess --gcp-project my-project --use-adc --format html,json --output reports/
```

Assess a specific engine, or let the tool recommend one (default assesses both):

```bash
bq-assess --gcp-project my-project --use-adc --engine athena   # or: redshift | both
```

BigQuery reservation details (for capacity-pricing customers) are auto-read during
collection; override the BigQuery-side baseline explicitly if needed:

```bash
bq-assess --gcp-project my-project --use-adc --bigquery-monthly-cost 12000
```

Assess every project you can access, with a cross-project summary:

```bash
bq-assess --gcp-project all --use-adc --format html,json --output reports/
```

See the [CLI Reference](docs/CLI_REFERENCE.md) for all flags and options.

## What You Get

- Two-axis scoring per entity:
  - **Migration Effort** (AUTO / ASSISTED / MANUAL) — data movement difficulty to S3 Tables
  - **Query Complexity** (PORTABLE / ADAPT / REWRITE) — SQL rewrite difficulty for the target engine
- **Engine recommendation** — Athena vs Redshift Serverless, justified from your actual
  workload profile (queries/day, bytes scanned, concurrency, latency SLA)
- **Per-entity storage placement** — S3 Tables (Iceberg) by default, with a Redshift
  Managed Storage hot-tier exception where the workload justifies it (ADR-0005)
- S3 Tables (Iceberg) DDL per table — Athena engine v3 and Redshift dialects
- Directional cost comparison (BigQuery vs AWS), clearly labelled by confidence
- HTML report (landing summary, effort breakdown, query detail) + matching JSON for automation

## How It Works

```
Preflight → Scan → Interpret
```

1. **Preflight** — checks tools and credentials, collects your project ID
2. **Scan** — runs the assessment CLI, streams progress
3. **Interpret** — reads the JSON report, highlights top effort/complexity entities and
   cost findings, points to the HTML report

## Documentation

- [Collect and Share](docs/COLLECT.md) — customer-facing collector guide
- [CLI Reference](docs/CLI_REFERENCE.md) — all flags, options, and examples
- [Migration Complexity Guide](docs/MIGRATION_COMPLEXITY_GUIDE.md) — two-axis scoring rules explained
- [Architecture Decision Records](docs/adr/) — why Iceberg storage, two scoring axes, partition mapping, per-entity placement
- [CONTEXT.md](CONTEXT.md) — project vocabulary and target architecture
- [PRIVACY.md](PRIVACY.md) — what the tool reads and where data goes

## Development

```bash
pip3 install -e ".[dev]"
pytest                           # 900+ tests (unit + property-based)
bash tests/plugin/structure.sh   # plugin structural checks
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.
