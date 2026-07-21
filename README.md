# sample-bigquery-to-aws-migration

Assess migrating a **Google BigQuery** warehouse to an **AWS lakehouse** — data in
**Amazon S3 Tables (Apache Iceberg)**, queried by **Amazon Redshift Serverless** or
**Amazon Athena**. The `bq-assess` CLI scans BigQuery metadata (read-only, never table
data), scores every table on two independent axes — **Migration Effort** (moving the
data) and **Query Complexity** (keeping the SQL running) — and generates Iceberg DDL,
load guidance, an engine recommendation, and a directional BigQuery-vs-AWS cost
comparison as HTML + JSON reports.

It **assesses; it does not execute the migration** — and it needs no AWS account to run.
Migration execution driven from the assessment output is on the roadmap.

## Quick Start (Claude Code)

The guided way to run an assessment. The skill handles setup, execution, and report
interpretation.

```
/plugin marketplace add aws-samples/sample-bigquery-to-aws-migration
/plugin install bq-assess@sample-bigquery-to-aws-migration
```

Then ask:

> "Assess BigQuery migration for project my-project"

## Prerequisites

- Python 3.9+
- [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed
- GCP authentication configured:

  ```bash
  gcloud auth application-default login
  ```

- IAM role on the target project: `roles/bigquery.metadataViewer`
- For query log analysis (optional, higher confidence): `roles/bigquery.resourceViewer`

## CLI Usage

Prefer the command line? Install directly:

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

For accurate cost estimates, add reservation details:

```bash
bq-assess --gcp-project my-project --use-adc \
  --reservation-config my-reservation-config.json \
  --format html,json --output reports/
```

See the [CLI Reference](docs/CLI_REFERENCE.md) for all flags and options.

## What You Get

- Two-axis scoring per entity:
  - **Migration Effort** (AUTO / ASSISTED / MANUAL) — data movement difficulty to S3 Tables
  - **Query Complexity** (PORTABLE / ADAPT / REWRITE) — SQL rewrite difficulty for the target engine
- **Engine recommendation** — Athena vs Redshift Serverless, justified from your actual
  workload profile (queries/day, bytes scanned, concurrency, latency SLA)
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

## Two Ways to Run

- **Direct** (`bq-assess`) — scan and generate the report in one step, inside the
  environment that has BigQuery access.
- **Collect, then report** (`bq-collect` + `bq-assess report`) — run the lightweight
  collector where the BigQuery credentials live; it writes a plain-JSON, checksummed
  bundle you can review before sharing. Generate the report later, anywhere, fully
  offline: `bq-assess report --bundle <dir-or-zip>`.

## Documentation

- [CLI Reference](docs/CLI_REFERENCE.md) — all flags, options, and examples
- [Migration Complexity Guide](docs/MIGRATION_COMPLEXITY_GUIDE.md) — two-axis scoring rules explained
- [Architecture Decision Records](docs/adr/) — why Iceberg storage, two scoring axes, partition mapping, per-entity placement
- [CONTEXT.md](CONTEXT.md) — project vocabulary and target architecture
- [PRIVACY.md](PRIVACY.md) — what the tool reads and where data goes

## Development

```bash
pip3 install -e ".[dev]"
pytest                           # 700+ tests (unit + property-based)
bash tests/plugin/structure.sh   # plugin structural checks
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.
