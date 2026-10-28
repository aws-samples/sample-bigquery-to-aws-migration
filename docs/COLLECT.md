# Collect and Share — Customer Guide

This page is for running the **collector** in your own BigQuery environment. It gathers
metadata only, writes a bundle of plain-JSON files you can inspect, and stops there.
Your AWS team generates the assessment report from that bundle and walks you through it.

Nothing leaves your environment until you choose to share the bundle.

## What the collector reads

- **Metadata only** — schemas, partitioning, clustering, view and routine definitions
  from `INFORMATION_SCHEMA`. It does **not** read your table data.
- **Aggregated usage statistics** — query counts, bytes scanned, slot usage.
- **Anonymized query text** — literals are stripped. Omit it entirely with
  `--exclude-query-text`.

## Prerequisites

- Python 3.9+ (already available in GCP Cloud Shell)
- Authentication — either:
  - `gcloud auth application-default login` (then use `--use-adc`), or
  - a service-account JSON key (then use `--credentials key.json`)
- IAM on the project you're scanning:
  - `roles/bigquery.metadataViewer` — required
  - `roles/bigquery.resourceViewer` — recommended; enables query-log analysis, which
    materially improves cost and complexity confidence
- If you use BigQuery **reservations** (capacity pricing): also grant
  `roles/bigquery.resourceViewer` on your reservation admin project so slot commitments
  can be read. The tool prompts and continues without it if unavailable.

## Run it

Cloud Shell is the easiest environment — it's already authenticated.

```bash
# 1. Install the collector (slim: no report-generation dependencies)
pip3 install "git+https://github.com/aws-samples/sample-bigquery-to-aws-migration.git@v0.10.0#subdirectory=packaging/collector"

# 2. Collect — a single project:
bq-collect --gcp-project <YOUR_PROJECT> --use-adc --output bundle-out/ --zip

# ...or every project your credentials can access, in one pass:
bq-collect --gcp-project all --use-adc --output bundle-out/ --zip
```

`--zip` writes an already-zipped bundle for you. With a single project that's
`bundle-out/bundle.zip`; with `all` it's one zip per project, each written to
`bundle-out/bundle-<project>/`. Datasets with no BigQuery usage are skipped
automatically, and one project failing (permissions, etc.) doesn't stop the rest.

Typical runs finish in under 10 minutes per project. Multi-region datasets are handled
automatically, and an interrupted scan resumes where it left off if you re-run it.

### Useful options

| Option | Effect |
| --- | --- |
| `--gcp-project all` | Collect every BigQuery project your credentials can access |
| `--zip` | Write the bundle already zipped, ready to send |
| `--datasets a,b,c` | Limit collection to specific datasets (single-project only) |
| `--exclude-query-text` | Collect usage statistics but no query text at all |
| `--skip-workload` | Skip query-log analysis entirely (lower confidence output) |
| `--credentials key.json` | Use a service-account key instead of ADC |
| `--concurrency 25` | Reduce parallel API requests if you hit quota limits |

Run `bq-collect --help` for the full list.

## Review before sharing

Before sending anything, unzip and look. Everything inside is plain text you can audit:

| File | Contents |
| --- | --- |
| `tables.json` | Table and view schemas, partitioning, clustering, sizes |
| `routines.json` | UDF and stored-procedure definitions |
| `workload.json` | Aggregated query statistics (counts, bytes, slots) |
| `pricing.json` | Detected BigQuery billing model (on-demand or capacity) |
| `rates.json` | Public price-list snapshot used for the comparison |
| `failures.json` | Any entities that could not be scanned, with reasons |
| `queries.jsonl` | Anonymized query text — absent if `--exclude-query-text` was used |
| `manifest.json` | File list with SHA-256 checksums, schema version, and regions scanned |

You are responsible for reviewing bundle contents before transmitting them outside your
environment. Open the files, check them, and share only if you're satisfied.

## Next step

Send `bundle.zip` to your AWS contact. They generate the report offline — no further
access to your environment is needed — and will walk you through the findings:
a directional BigQuery-vs-AWS cost comparison, an engine recommendation, per-table
migration effort and query-complexity scoring, and generated Iceberg DDL.

## Questions

The full source of this tool is public in this repository — you're welcome to read it.
See [PRIVACY.md](../PRIVACY.md) for details on what the tool reads and where data goes,
and the [CLI Reference](CLI_REFERENCE.md) for every available flag.
