"""Unit tests for reservation auto-reader."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bq_assess.core.reservation_reader import (
    Commitment,
    ReservationCache,
    ReservationReadResult,
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
        mock_row.max_slots = 400

        mock_query_job = MagicMock()
        mock_query_job.result.return_value = iter([mock_row])

        mock_client = MagicMock()
        mock_client.query.return_value = mock_query_job

        commitments = [Commitment(slot_count=500, plan="ANNUAL", edition="ENTERPRISE")]
        with (
            patch("bq_assess.core.reservation_reader._read_commitments", return_value=commitments),
            patch("bq_assess.core.reservation_reader._read_timeline", return_value=(10000, 86400)),
            patch("bq_assess.core.reservation_reader._read_assignments", return_value=(["proj-a", "proj-b"], 2)),
        ):
            result = read_reservation_details(mock_client, "admin-proj", "us", "my-res")

        assert result.success is True
        assert result.baseline_slots == 200
        assert result.max_slots == 400
        assert result.edition == "ENTERPRISE"
        assert result.commitment_slots == 500
        assert result.commitment_plan == "ANNUAL"
        assert result.autoscale_slot_seconds == 10000
        assert result.timeline_window_seconds == 86400
        assert result.assigned_projects == ["proj-a", "proj-b"]
        assert result.assigned_count == 2

    def test_not_found(self):
        mock_query_job = MagicMock()
        mock_query_job.result.return_value = iter([])

        mock_client = MagicMock()
        mock_client.query.return_value = mock_query_job

        result = read_reservation_details(mock_client, "admin-proj", "us", "nonexistent")
        assert result.success is False
        assert result.permission_denied is False
        assert "not found" in result.error_message.lower()

    def test_timeline_returns_calendar_window_not_count(self):
        """_read_timeline returns lookback_days×86400 as window, not COUNT(*)."""
        from bq_assess.core.reservation_reader import _read_timeline

        mock_row = MagicMock()
        mock_row.total_autoscale_slot_seconds = 5000

        mock_query_job = MagicMock()
        mock_query_job.result.return_value = iter([mock_row])

        mock_client = MagicMock()
        mock_client.query.return_value = mock_query_job

        autoscale, window = _read_timeline(
            mock_client, "admin-proj", "us", "my-res", lookback_days=7
        )
        assert autoscale == 5000
        assert window == 7 * 86400  # calendar seconds, not sparse COUNT(*)

    def test_api_error(self):
        from google.api_core.exceptions import NotFound
        mock_client = MagicMock()
        mock_client.query.return_value.result.side_effect = NotFound("404")

        result = read_reservation_details(mock_client, "admin-proj", "us", "my-res")
        assert result.success is False
        assert result.permission_denied is False


class TestReservationCache:
    def test_put_and_get_success_result(self):
        cache = ReservationCache()
        result = ReservationReadResult(success=True, baseline_slots=100)
        cache.put("admin-proj", "US", "my-res", result)
        assert cache.get("admin-proj", "US", "my-res") is result

    def test_denied_result_is_cached_and_retrievable(self):
        """Permission-denied results must be cached to avoid redundant API calls in fleet mode."""
        cache = ReservationCache()
        denied = ReservationReadResult(success=False, permission_denied=True, admin_project="admin-proj")
        cache.put("admin-proj", "US", "my-res", denied)

        # The denied result should be retrievable (prevents redundant 403 calls)
        cached = cache.get("admin-proj", "US", "my-res")
        assert cached is denied
        assert cached.permission_denied is True

        # It should also appear in the denied list
        assert "admin-proj" in cache.denied_admin_projects()

    def test_get_returns_none_for_unknown(self):
        cache = ReservationCache()
        assert cache.get("unknown", "US", "res") is None

    def test_location_case_insensitive(self):
        cache = ReservationCache()
        result = ReservationReadResult(success=True, baseline_slots=50)
        cache.put("proj", "Us-Central1", "res", result)
        assert cache.get("proj", "us-central1", "res") is result
        assert cache.get("proj", "US-CENTRAL1", "res") is result

    def test_print_fleet_denied_summary_no_denied(self):
        cache = ReservationCache()
        mock_console = MagicMock()
        cache.print_fleet_denied_summary(mock_console)
        mock_console.print.assert_not_called()

    def test_print_fleet_denied_summary_with_denied(self):
        cache = ReservationCache()
        denied = ReservationReadResult(success=False, permission_denied=True, admin_project="proj-a")
        cache.put("proj-a", "US", "res", denied)
        mock_console = MagicMock()
        cache.print_fleet_denied_summary(mock_console)
        assert mock_console.print.call_count >= 3  # at least the 3 print lines
