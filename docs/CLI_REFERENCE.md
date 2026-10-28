# bq-assess CLI Reference

For the guided experience, see the [main README](../README.md).

## Install

```bash
pip3 install "git+https://github.com/aws-samples/sample-bigquery-to-aws-migration.git@v0.10.0"
```

## Usage

```bash
# Minimal
bq-assess --gcp-project my-project --use-adc

# With query logs (higher confidence)
bq-assess --gcp-project my-project --use-adc

# With exported query logs (no bigquery.jobs.listAll needed)
bq-assess --gcp-project my-project --use-adc --query-logs path/to/logs.json

# Full options
bq-assess \
  --gcp-project my-project \
  --use-adc \
  --datasets "prod_data,analytics" \
  --query-log-days 60 \
  --format json,html,csv \
  --output reports/

# From config file
bq-assess --config assessment-config.yaml
```

## Options

| Flag | Description | Default |
|------|-------------|---------|
| `--gcp-project` | GCP project ID | required |
| `--credentials` | Path to service account JSON | — |
| `--use-adc` | Use Application Default Credentials | false |
| `--datasets` | Comma-separated dataset filter | all datasets |
| `--zip` | Write the exported bundle as bundle.zip (hand-off artifact) | false |
| `--query-logs` | Path to exported query logs JSON | — |
| `--query-log-days` | Query log lookback window in days (1-90) | 30 |
| `--redshift-type` | Target Redshift node type (overrides auto-recommendation) | auto |
| `--bigquery-monthly-cost` | Monthly BigQuery spend override | estimated |
| `--output` | Output directory — see [Output layout](#output-layout) | bq-migration/ |
| `--format` | Output formats (json, html, csv) | json,html |
| `--config` | Path to YAML config file | — |

Note: query-log analysis is always on; opt out with `--skip-workload` (no JOBS reads) or `--exclude-query-text` (no statements in the bundle).

## Output layout

Each run writes a **new** folder under `--output`, named `<project>_<YYYY-MM-DD>_<HHMMSS>` (UTC), and never modifies earlier ones. Runs started in the same second get a `-2`, `-3` suffix. When the run finishes, it updates `latest-<project>.json` in the output directory, which names that run's files (paths relative to the output directory):

```
<output>/
  latest-<project>.json
  <project>_<YYYY-MM-DD>_<HHMMSS>/
    README.html
    report/      assessment-{landing,effort,query}-<id>.json, <project>-assessment.html
    terraform/   migration/   bundle/   query-workload/
```

Scripts and integrations should read `latest-<project>.json` rather than search for report files by name. If you have already run `terraform apply` from one run's folder, keep running Terraform from that folder — its state file lives there, and a newer run's `terraform/` starts without it.

Before 0.10.0 the folder was `<project>_<YYYY-MM-DD>`, shared by every run that day, and no pointer file was written.

Note: bq-assess never prompts for configuration — run
`bq-assess assess --gcp-project <X|all> --use-adc` and it completes on its own
(the `--interactive` flag was removed 2026-08-12). The one exception: when a
capacity reservation read is permission-denied in a terminal, it offers
"Grant access and retry?" so the collection needn't be re-run. `bq-assess
report`, fleet runs, and scripted/redirected runs never prompt at all.

## Query Logs

Without query logs, the tool uses heuristics for DISTKEY/SORTKEY recommendations (LOW confidence).

Query-log analysis (always on) analyzes `INFORMATION_SCHEMA.JOBS` for JOIN patterns and hub tables (HIGH confidence). Requires `roles/bigquery.resourceViewer`.

Alternatively, `--query-logs <path>` accepts an exported JSON file — a JSON array of objects with at least a `"query"` field. This bypasses the API entirely.
