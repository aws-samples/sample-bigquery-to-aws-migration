"""Unit tests for reservation auto-reader."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bq_assess.core.reservation_reader import (
    parse_admin_project,
    read_reservation_details,
)


class TestParseAdminProject:
    def test_standard_format(self):
        assert parse_admin_project("my-admin-project:us.prod-reservation") == (
            "my-admin-project", "us", "prod-reservation"
        )

    def test_region_with_dashes(self):
        assert parse_admin_project("proj-123:australia-southeast1.analytics") == (
            "proj-123", "australia-southeast1", "analytics"
        )

    def test_none(self):
        assert parse_admin_project(None) is None

    def test_empty_string(self):
        assert parse_admin_project("") is None

    def test_no_colon(self):
        assert parse_admin_project("no-colon-here") is None

    def test_no_dot(self):
        assert parse_admin_project("project:no-dot") is None

    def test_multiple_dots_takes_first(self):
        result = parse_admin_project("proj:us-central1.res.name.extra")
        assert result == ("proj", "us-central1", "res.name.extra")


class TestReadReservationDetails:
    def test_permission_denied(self):
        from google.api_core.exceptions import Forbidden
        mock_client = MagicMock()
        mock_client.query.return_value.result.side_effect = Forbidden("Access Denied")

        result = read_reservation_details(mock_client, "admin-proj", "us", "my-res")
        assert result.success is False
        assert result.permission_denied is True
        assert result.baseline_slots is None

    def test_success(self):
        mock_row = MagicMock()
        mock_row.slot_capacity = 200
        mock_row.edition = "ENTERPRISE"

        mock_query_job = MagicMock()
        mock_query_job.result.return_value = iter([mock_row])

        mock_client = MagicMock()
        mock_client.query.return_value = mock_query_job

        with patch("bq_assess.core.reservation_reader._read_commitments", return_value=(500, "ANNUAL")):
            result = read_reservation_details(mock_client, "admin-proj", "us", "my-res")

        assert result.success is True
        assert result.baseline_slots == 200
        assert result.edition == "ENTERPRISE"
        assert result.commitment_slots == 500
        assert result.commitment_plan == "ANNUAL"

    def test_not_found(self):
        mock_query_job = MagicMock()
        mock_query_job.result.return_value = iter([])

        mock_client = MagicMock()
        mock_client.query.return_value = mock_query_job

        result = read_reservation_details(mock_client, "admin-proj", "us", "nonexistent")
        assert result.success is False
        assert result.permission_denied is False
        assert "not found" in result.error_message.lower()

    def test_api_error(self):
        from google.api_core.exceptions import NotFound
        mock_client = MagicMock()
        mock_client.query.return_value.result.side_effect = NotFound("404")

        result = read_reservation_details(mock_client, "admin-proj", "us", "my-res")
        assert result.success is False
        assert result.permission_denied is False
