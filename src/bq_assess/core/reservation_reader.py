"""Auto-read BigQuery reservation details from the admin project.

Parses the admin project from reservation_id (format:
"ADMIN_PROJECT:LOCATION.RESERVATION_NAME"), queries INFORMATION_SCHEMA.RESERVATIONS,
CAPACITY_COMMITMENTS, RESERVATIONS_TIMELINE, and ASSIGNMENTS in that project.
Requires roles/bigquery.resourceViewer on the admin project.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class Commitment:
    """A single active capacity commitment."""
    slot_count: int
    plan: str
    edition: str | None = None
    end_time: datetime | None = None


@dataclass
class ReservationReadResult:
    success: bool
    permission_denied: bool = False
    baseline_slots: int | None = None
    max_slots: int | None = None
    edition: str | None = None
    commitment_slots: int | None = None
    commitment_plan: str | None = None
    commitments: list[Commitment] = field(default_factory=list)
    autoscale_slot_seconds: int | None = None
    timeline_window_seconds: int | None = None
    assigned_projects: list[str] = field(default_factory=list)
    assigned_count: int = 0
    error_message: str | None = None
    admin_project: str | None = None


class ReservationCache:
    """Shares reservation reads across --gcp-project all runs."""

    def __init__(self):
        self._cache: dict[tuple[str, str, str], ReservationReadResult] = {}
        self._denied: set[str] = set()

    def get(self, admin_project: str, location: str, reservation_name: str) -> ReservationReadResult | None:
        return self._cache.get((admin_project, location.lower(), reservation_name))

    def put(self, admin_project: str, location: str, reservation_name: str, result: ReservationReadResult):
        self._cache[(admin_project, location.lower(), reservation_name)] = result
        if result.permission_denied:
            self._denied.add(admin_project)

    def denied_admin_projects(self) -> list[str]:
        """Return admin projects where permission was denied."""
        return list(self._denied)

    def print_fleet_denied_summary(self, console) -> None:
        """Print a summary of denied admin projects if any exist.

        Used by both bq-assess and bq-collect fleet modes to avoid duplicating
        the same reporting block.
        """
        denied = self.denied_admin_projects()
        if not denied:
            return
        console.print()
        console.print(
            f"[yellow]⚠ Reservation details were UNAVAILABLE for admin project(s): "
            f"{', '.join(denied)}[/yellow]"
        )
        console.print(
            "[yellow]  Affected projects: ENTERPRISE/ENTERPRISE_PLUS show capacity cost as UNAVAILABLE;[/yellow]"
        )
        console.print(
            "[yellow]  STANDARD falls back to a modelled range from measured slot usage.[/yellow]"
        )


def parse_admin_project(reservation_id: str | None) -> tuple | None:
    """Parse 'ADMIN_PROJECT:LOCATION.RESERVATION_NAME' → (project, location, name).

    Returns None if reservation_id is None or malformed.
    """
    if not reservation_id:
        return None
    if ":" not in reservation_id:
        return None
    project, rest = reservation_id.split(":", 1)
    if "." not in rest:
        return None
    location, name = rest.split(".", 1)
    return (project, location, name)


def read_reservation_details(
    client, admin_project: str, location: str, reservation_name: str,
    lookback_days: int = 30,
) -> ReservationReadResult:
    """Query INFORMATION_SCHEMA.RESERVATIONS in the admin project.

    Also reads CAPACITY_COMMITMENTS (all active), RESERVATIONS_TIMELINE
    (autoscale slot-seconds), and ASSIGNMENTS (shared projects).

    Returns a ReservationReadResult. On permission denied, sets
    permission_denied=True so the caller can offer a retry.
    """
    from google.api_core.exceptions import Forbidden, GoogleAPICallError, NotFound
    from google.cloud.bigquery import QueryJobConfig, ScalarQueryParameter

    query = (
        "SELECT slot_capacity, edition, autoscale.max_slots AS max_slots "
        f"FROM `{admin_project}.region-{location.lower()}`.INFORMATION_SCHEMA.RESERVATIONS "
        "WHERE reservation_name = @res_name"
    )
    job_config = QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("res_name", "STRING", reservation_name),
        ]
    )

    try:
        result = client.query(
            query, job_config=job_config, project=admin_project, location=location
        ).result()
        rows = list(result)
        if not rows:
            return ReservationReadResult(
                success=False, admin_project=admin_project,
                error_message=f"Reservation '{reservation_name}' not found in {admin_project}",
            )
        row = rows[0]
        baseline = getattr(row, "slot_capacity", None)
        edition = getattr(row, "edition", None)
        max_slots = getattr(row, "max_slots", None)

        commitments = _read_commitments(client, admin_project, location)
        total_commitment_slots = sum(c.slot_count for c in commitments) if commitments else None
        largest_plan = commitments[0].plan if commitments else None

        autoscale_slot_seconds, timeline_window_seconds = _read_timeline(
            client, admin_project, location, reservation_name, lookback_days
        )

        assigned_projects, assigned_count = _read_assignments(
            client, admin_project, location, reservation_name
        )

        return ReservationReadResult(
            success=True, admin_project=admin_project,
            baseline_slots=baseline, max_slots=max_slots,
            edition=edition,
            commitment_slots=total_commitment_slots,
            commitment_plan=largest_plan,
            commitments=commitments,
            autoscale_slot_seconds=autoscale_slot_seconds,
            timeline_window_seconds=timeline_window_seconds,
            assigned_projects=assigned_projects,
            assigned_count=assigned_count,
        )
    except Forbidden as exc:
        return ReservationReadResult(
            success=False, permission_denied=True, admin_project=admin_project,
            error_message=str(exc),
        )
    except (NotFound, GoogleAPICallError) as exc:
        return ReservationReadResult(
            success=False, admin_project=admin_project,
            error_message=str(exc),
        )


def _read_commitments(
    client, admin_project: str, location: str
) -> list[Commitment]:
    """Read ALL active capacity commitments — returns list sorted by slot_count desc."""
    query = (
        "SELECT slot_count, plan, edition, commitment_end_time "
        f"FROM `{admin_project}.region-{location.lower()}`.INFORMATION_SCHEMA.CAPACITY_COMMITMENTS "
        "WHERE state = 'ACTIVE' "
        "ORDER BY slot_count DESC"
    )
    try:
        result = client.query(query, project=admin_project, location=location).result()
        commitments = []
        for row in result:
            commitments.append(Commitment(
                slot_count=getattr(row, "slot_count", 0),
                plan=getattr(row, "plan", "UNKNOWN"),
                edition=getattr(row, "edition", None),
                end_time=getattr(row, "commitment_end_time", None),
            ))
        return commitments
    except Exception:
        logger.debug("CAPACITY_COMMITMENTS read failed for %s", admin_project, exc_info=True)
    return []


def _read_timeline(
    client, admin_project: str, location: str,
    reservation_name: str, lookback_days: int,
) -> tuple[int | None, int | None]:
    """Read RESERVATIONS_TIMELINE for autoscale slot-seconds over lookback window.

    Returns (total_autoscale_slot_seconds, calendar_window_seconds) or (None, None).
    The window is the known calendar span (lookback_days × 86400), NOT COUNT(*) of
    per_second_details records — that array is sparse (only seconds with activity).
    """
    query = (
        "SELECT "
        "  SUM(s.autoscale_current_slots) AS total_autoscale_slot_seconds "
        f"FROM `{admin_project}.region-{location.lower()}`.INFORMATION_SCHEMA.RESERVATIONS_TIMELINE t, "
        "  UNNEST(t.per_second_details) s "
        "WHERE reservation_name = @res_name "
        "  AND period_start >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)"
    )
    from google.cloud.bigquery import QueryJobConfig, ScalarQueryParameter
    job_config = QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("res_name", "STRING", reservation_name),
            ScalarQueryParameter("days", "INT64", lookback_days),
        ]
    )
    try:
        result = client.query(
            query, job_config=job_config, project=admin_project, location=location
        ).result()
        rows = list(result)
        if rows and rows[0].total_autoscale_slot_seconds is not None:
            return (
                rows[0].total_autoscale_slot_seconds or 0,
                lookback_days * 86400,
            )
    except Exception:
        logger.debug("RESERVATIONS_TIMELINE read failed for %s", admin_project, exc_info=True)
    return (None, None)


def _read_assignments(
    client, admin_project: str, location: str, reservation_name: str,
) -> tuple[list[str], int]:
    """Read ASSIGNMENTS to determine which projects/folders share this reservation.

    Returns (list_of_assignee_ids, total_count).
    """
    query = (
        "SELECT assignee_id, assignee_type "
        f"FROM `{admin_project}.region-{location.lower()}`.INFORMATION_SCHEMA.ASSIGNMENTS "
        "WHERE reservation_name = @res_name"
    )
    from google.cloud.bigquery import QueryJobConfig, ScalarQueryParameter
    job_config = QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("res_name", "STRING", reservation_name),
        ]
    )
    try:
        result = client.query(
            query, job_config=job_config, project=admin_project, location=location
        ).result()
        assignees = []
        for row in result:
            assignees.append(getattr(row, "assignee_id", ""))
        return (assignees, len(assignees))
    except Exception:
        logger.debug("ASSIGNMENTS read failed for %s", admin_project, exc_info=True)
    return ([], 0)
