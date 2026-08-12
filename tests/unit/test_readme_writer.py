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


class TestFleetReadme:
    def test_fleet_readme_written_next_to_summary(self, tmp_path):
        from bq_assess.report.readme_writer import write_fleet_readme
        path = write_fleet_readme(str(tmp_path), [
            ("proj-a", "proj-a_2026-08-03"),
            ("proj-b", "proj-b_2026-08-03"),
        ])
        assert path == str(tmp_path / "README.html")
        content = Path(path).read_text(encoding="utf-8")
        # SUMMARY-first reading order
        assert "SUMMARY.html" in content
        # project links
        assert 'proj-a_2026-08-03/report/proj-a-assessment.html' in content
        assert "proj-b" in content
        # shared sections present (single source with the per-project README)
        assert "bigquery.tables.get" in content
        assert "roles/bigquery.readSessionUser" in content
        assert "Owner/Editor" in content            # troubleshooting table
        # folder-structure section listed once
        assert "Inside Each Project Folder" in content

    def test_fleet_and_single_share_permission_sections(self, tmp_path):
        """The shared sections must be identical in both modes (no drift)."""
        import re

        from bq_assess.report.readme_writer import write_fleet_readme

        single = Path(write_readme(str(tmp_path / "p"), "proj-a")).read_text(encoding="utf-8") \
            if (tmp_path / "p").mkdir() is None else ""
        fleet = Path(write_fleet_readme(str(tmp_path), [("proj-a", "f")])).read_text(encoding="utf-8")

        def section(html, start, end):
            block = html[html.index(start):html.index(end)]
            # normalize the one legitimate difference: the project id in commands
            return re.sub(r"proj-a|YOUR_PROJECT_ID", "PROJECT", block)

        s1 = section(single, "GCP Permissions Required", "About This Assessment")
        s2 = section(fleet, "GCP Permissions Required", "About This Assessment")
        assert s1 == s2


def test_fleet_readme_anatomy_matches_shipped_folders(tmp_path):
    """2026-08-04 audit: fleet README listed a bundle/ that wasn't shipped and
    omitted query-workload/ entirely."""
    from bq_assess.report.readme_writer import write_fleet_readme

    path = write_fleet_readme(str(tmp_path), [("proj-a", "proj-a_2026-08-04")])
    from pathlib import Path
    html = Path(path).read_text()
    assert "query-workload/" in html
    assert "bundle/" not in html
    assert "rebuilt_entities.sql" in html
