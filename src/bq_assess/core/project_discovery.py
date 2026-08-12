"""GCP project discovery shared by bq-assess and bq-collect.

Lives in core/ (not cli.py) because the collector distribution ships only
bq_assess + bundle + core — the `--gcp-project all` fan-out must work in both
tools (customers run the collector unattended across their estate).
"""
from __future__ import annotations


def discover_projects(credentials_path: str | None) -> list[tuple[str, bool]]:
    """List all GCP projects the caller can access that have BigQuery enabled.

    Uses bigquery.Client.list_projects() — returns only projects visible to the
    active credentials with the BigQuery API enabled. Each entry is
    (project_id, has_datasets) so empty projects can be skipped up front.
    """
    from google.cloud import bigquery
    from google.oauth2 import service_account

    if credentials_path:
        creds = service_account.Credentials.from_service_account_file(credentials_path)
        client = bigquery.Client(credentials=creds, project=creds.project_id)
    else:
        # ADC: project is irrelevant for list_projects; use any placeholder the
        # credential resolves. bigquery.Client() picks up the ADC default.
        client = bigquery.Client()

    projects: list[tuple[str, bool]] = []
    for p in sorted(client.list_projects(), key=lambda p: p.project_id):
        try:
            has_datasets = any(client.list_datasets(project=p.project_id, max_results=1))
        except Exception:
            has_datasets = False  # can't list — treated as empty, surfaced as SKIPPED
        projects.append((p.project_id, has_datasets))
    return projects
