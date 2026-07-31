"""Tests for the customer-facing README.html generator."""
import os
from pathlib import Path

import pytest

from bq_assess.report.readme_writer import write_readme


@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "my-project_2026-07-30"
    d.mkdir()
    return str(d)


def test_readme_written_with_all_sections(output_dir):
    path = write_readme(
        output_dir,
        gcp_project="acme-analytics-prod",
        has_report=True,
        has_terraform=True,
        has_migration=True,
        has_bundle=True,
        has_rebuilt_entities=True,
        has_redshift_phase=True,
    )
    assert os.path.exists(path)
    assert path.endswith("README.html")
    content = Path(path).read_text(encoding="utf-8")

    # GCP permissions documented
    assert "bigquery.tables.get" in content
    assert "bigquery.tables.list" in content
    assert "bigquery.jobs.listAll" in content
    assert "bigquery.reservations.list" in content
    assert "bigquery.readsessions.create" in content

    # Roles mentioned
    assert "roles/bigquery.metadataViewer" in content
    assert "roles/bigquery.resourceViewer" in content
    assert "roles/bigquery.jobUser" in content
    assert "roles/bigquery.readSessionUser" in content

    # Error messages documented
    assert "INFORMATION_SCHEMA.TABLE_STORAGE" in content
    assert "403" in content

    # Directory listing
    assert "report/" in content
    assert "terraform/" in content
    assert "migration/" in content
    assert "bundle/" in content

    # Conditional sections present
    assert "rebuilt_entities.sql" in content
    assert "redshift_phase.sql" in content

    # Project name injected
    assert "acme-analytics-prod" in content


def test_readme_without_optional_sections(output_dir):
    path = write_readme(
        output_dir,
        gcp_project="simple-project",
        has_report=True,
        has_terraform=True,
        has_migration=True,
        has_bundle=True,
        has_rebuilt_entities=False,
        has_redshift_phase=False,
    )
    content = Path(path).read_text(encoding="utf-8")
    assert "rebuilt_entities.sql" not in content
    assert "redshift_phase.sql" not in content


def test_readme_always_has_permissions(output_dir):
    """Even a minimal README documents the required GCP permissions."""
    path = write_readme(
        output_dir,
        gcp_project="minimal-proj",
        has_report=True,
        has_terraform=True,
        has_migration=True,
        has_bundle=True,
    )
    content = Path(path).read_text(encoding="utf-8")
    assert "bigquery.tables.get" in content
    assert "bigquery.jobs.listAll" in content
    assert "roles/bigquery.metadataViewer" in content
    assert "INFORMATION_SCHEMA.TABLE_STORAGE" in content
    assert "Re-Running the Assessment" in content
