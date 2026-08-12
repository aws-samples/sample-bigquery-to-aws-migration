# Scan Phase

## Entry Assertions

The Preflight phase has completed successfully. All of the following are true:

- `gcp_project` is set and non-empty.
- GCP authentication is confirmed (`adc_present` was `true` in preflight).
- `bq-assess` CLI is installed and on PATH.
- Optional parameters may also be present: `datasets`, `skip_workload`, `query_log_days`.
- If the user says "stop" or "cancel" at any point during this phase, exit the skill immediately without running any further commands.

## Step 1: Construct CLI Command

Build the `bq-assess` command from the parameters collected during Preflight.

**Base command:**

```
bq-assess --gcp-project {gcp_project} --use-adc --format json,html --output reports/
```

**Append optional flags based on Preflight output:**

- If `datasets` was provided:
  ```
  --datasets {datasets}
  ```

- Query-log analysis is **always on** (since v0.6.2, for collection parity with `bq-collect`). There is NO `--include-query-logs` flag — passing it fails with a usage error. There is no `--no-query-logs` flag either; do not invent one.

- If `skip_workload` is `true` (the user opted out of workload analysis):
  ```
  --skip-workload
  ```

- If `query_log_days` was provided (only meaningful when `skip_workload` is `false`):
  ```
  --query-log-days {query_log_days}
  ```

- If the user already has query logs exported to a JSON file (instead of reading `INFORMATION_SCHEMA.JOBS` live):
  ```
  --query-logs {path}
  ```

**Example — full command with all options:**

```bash
bq-assess --gcp-project my-project --use-adc --format json,html --output reports/ --datasets prod_data,analytics --query-log-days 60
```

**Example — minimal command (no dataset filter; query logs are read automatically):**

```bash
bq-assess --gcp-project my-project --use-adc --format json,html --output reports/
```

Show the constructed command to the user before executing so they can confirm or adjust.

## Step 2: Execute CLI

Run the constructed command using the Bash tool.

**Critical: Display the CLI's Rich progress output directly to the user.** The CLI uses Rich to render stage-by-stage progress (scanning metadata, analyzing query logs, scoring complexity, generating reports). Do **NOT** suppress, parse, or reformat these progress messages. Let them stream through as-is so the user can follow along and explain progress if a customer is watching.

## Step 3: Handle Results

After the CLI exits, inspect the exit code and output to determine the next action.

---

### Success (exit code 0)

The CLI has completed successfully and written reports to the output directory. The run produces **three mirrored JSON files** (not one) — capture all three:

1. Locate the three report files in the output directory:
   - `landing_json` — matches `reports/assessment-landing-*.json` (holds `summary` + `cost`)
   - `effort_json` — matches `reports/assessment-effort-*.json` (holds Migration Effort `entities[]` + Iceberg DDL)
   - `query_json` — matches `reports/assessment-query-*.json` (holds Query Complexity `entities[]`)
2. Verify each file exists and is non-empty. If `landing_json` is missing or empty, treat this as a generic fatal error (see below). If only `effort_json`/`query_json` is missing, note it and continue with what is present.
3. **Check for silently degraded workload data** — see "Workload Data Unavailable" below. Do this BEFORE describing the run as clean.
4. Tell the user the assessment completed successfully and show the output directory path.
5. Advance to the **Interpret phase**, passing `landing_json`, `effort_json`, and `query_json`.

---

### Workload Data Unavailable (exit code 0, but no query-log data)

**Detection:** after a successful run, read the `cost` block of `landing_json`. Workload data is missing when `cost.estimate_basis` contains the string `"No workload data"` (the CLI's own wording).

**Why this needs handling:** query-log analysis is always attempted and **degrades silently**. A missing `bigquery.jobs.listAll` permission does NOT fail the run — the CLI exits 0 and writes a complete-looking report whose cost figures are unmeasured. There is no `AnalyzerError` to catch. Do not present this as a clean result.

Tell the user plainly:

- "The assessment completed, but query-log analysis returned no data — most often a missing `bigquery.jobs.listAll` permission. The cost comparison is a rough range rather than a measured estimate, and Query Complexity scoring is heuristic-only."

Then offer **two options:**

#### Option 1: Grant the permission and re-run for a measured estimate

```bash
gcloud projects add-iam-policy-binding {gcp_project} \
  --member="user:{sa_email}" \
  --role="roles/bigquery.resourceViewer"
```

> Replace `{sa_email}` with the user's GCP identity (`gcloud auth list` shows it). `roles/bigquery.resourceViewer` carries `bigquery.jobs.listAll`.

After the grant lands, re-run the same command from Step 2.

#### Option 2: Continue with the current report

Advance to the Interpret phase, and carry the caveat forward: the Interpret summary MUST state that cost confidence is reduced because no workload data was available.

Ask: **"Would you like to (1) grant the permission and re-run for measured numbers, or (2) continue with this report as-is?"**

If the user already has logs exported to a file, a third path exists: re-run with `--query-logs {path}`.

---

### Credential Error

**Detection:** stderr contains `ScannerError` from `validate_credentials()` **OR** stderr contains `google.auth.exceptions.RefreshError`.

The GCP credentials are invalid, expired, or not properly configured.

Tell the user: "Your GCP credentials appear to be invalid or expired. Let's go back to the authentication step."

Route back to the **Preflight phase** — specifically, load `references/phases/preflight.md` and resume at **Step 3: Handle Missing ADC**. The user will need to re-run:

```bash
gcloud auth application-default login
```

After re-authentication, return to this Scan phase and retry the CLI command.

---

### No Tables Found

**Detection:** CLI output contains `"No tables found. Nothing to assess."`.

The CLI found no BigQuery tables to assess in the specified project/datasets.

Tell the user: "The CLI found no tables to assess. This usually means the `--datasets` filter is too narrow or the project ID is incorrect."

Prompt the user to:
1. Double-check the `gcp_project` value.
2. If `datasets` was specified, verify the dataset names exist in the project.
3. Try running without the `--datasets` filter to scan all datasets.

Do **NOT** advance to the Interpret phase. Offer to re-run with corrected parameters.

---

### Generic Fatal Error

**Detection:** Non-zero exit code AND none of the specific patterns above matched.

Display the error message **verbatim** to the user. Do not summarize, truncate, or reformat it — the user may need the full output for debugging or to share with the tool maintainer.

Then ask: **"Would you like to retry the assessment, or cancel?"**

- If **retry**: re-run the same command from Step 2.
- If **cancel**: exit the skill.

## Cancellation

If the user says "stop" or "cancel" at any point during this phase — including while the CLI is running, while reviewing error options, or after seeing results — exit the skill immediately. Do not run additional commands or write files.

## Exit Conditions

The Scan phase is complete when **all** of the following are true:

1. The CLI exited with code 0.
2. `landing_json` is set to a valid file path that exists and is non-empty.
3. `effort_json` and `query_json` are captured if present (note any that are missing).

Pass `landing_json`, `effort_json`, and `query_json` to the **Interpret phase**.
