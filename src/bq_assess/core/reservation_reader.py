"""Auto-read BigQuery reservation details from the admin project.

Parses the admin project from reservation_id (format:
"ADMIN_PROJECT:LOCATION.RESERVATION_NAME"), queries INFORMATION_SCHEMA.RESERVATIONS
in that project, and returns baseline/max slots. Requires
roles/bigquery.resourceViewer on the admin project.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ReservationReadResult:
    success: bool
    permission_denied: bool = False
    baseline_slots: int | None = None
    max_slots: int | None = None
    edition: str | None = None
    commitment_slots: int | None = None
    commitment_plan: str | None = None
    error_message: str | None = None
    admin_project: str | None = None


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
    client, admin_project: str, location: str, reservation_name: str
) -> ReservationReadResult:
    """Query INFORMATION_SCHEMA.RESERVATIONS in the admin project.

    Returns a ReservationReadResult. On permission denied, sets
    permission_denied=True so the caller can offer a retry.
    """
    from google.api_core.exceptions import Forbidden, GoogleAPICallError, NotFound
    from google.cloud.bigquery import QueryJobConfig, ScalarQueryParameter

    query = (
        f"SELECT slot_capacity, edition "
        f"FROM `{admin_project}.region-{location.lower()}`.INFORMATION_SCHEMA.RESERVATIONS "
        f"WHERE reservation_name = @res_name"
    )
    job_config = QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("res_name", "STRING", reservation_name),
        ]
    )

    try:
        # location= pins the query job to the reservation's region — region-
        # qualified INFORMATION_SCHEMA views only resolve when the job runs
        # there; without it an EU admin project silently returns no rows
        # (2026-07-23 TABLE_STORAGE bug class, fixed here 2026-07-28).
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

        commitment_slots, commitment_plan = _read_commitments(
            client, admin_project, location
        )

        return ReservationReadResult(
            success=True, admin_project=admin_project,
            baseline_slots=baseline, max_slots=None,
            edition=edition, commitment_slots=commitment_slots,
            commitment_plan=commitment_plan,
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
) -> tuple:
    """Best-effort read of CAPACITY_COMMITMENTS — returns (slots, plan) or (None, None)."""
    query = (
        f"SELECT slot_count, plan "
        f"FROM `{admin_project}.region-{location.lower()}`.INFORMATION_SCHEMA.CAPACITY_COMMITMENTS "
        f"WHERE state = 'ACTIVE' "
        f"ORDER BY slot_count DESC LIMIT 1"
    )
    try:
        # location= pins the job to the commitments' region (see read_reservation_details).
        result = client.query(query, project=admin_project, location=location).result()
        rows = list(result)
        if rows:
            return (getattr(rows[0], "slot_count", None), getattr(rows[0], "plan", None))
    except Exception:
        logger.debug("CAPACITY_COMMITMENTS read failed for %s", admin_project, exc_info=True)
    return (None, None)
