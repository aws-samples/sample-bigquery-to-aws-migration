"""Storage Read API egress estimation via Cloud Monitoring (R18-egress).

BigQuery Storage Read API (used by pandas .to_dataframe(), Spark connectors, and any
direct table read) creates egress traffic that is INVISIBLE to INFORMATION_SCHEMA.JOBS.
This module queries Cloud Monitoring for CreateReadSession counts and infers monthly
egress volume using the formula:

    egress_gib = read_sessions × (total_logical_gib / table_count)

Requires roles/monitoring.viewer on the project. If unavailable, returns None and the
caller prints a terminal warning + adds a scope note.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

_log = logging.getLogger(__name__)


@dataclass
class EgressEstimate:
    """Result of the Storage Read API egress estimation."""
    read_sessions: int
    estimated_egress_gib: float
    lookback_days: int


def estimate_storage_api_egress(
    project_id: str,
    total_logical_bytes: int,
    table_count: int,
    *,
    lookback_days: int = 30,
    credentials=None,
) -> EgressEstimate | None:
    """Query Cloud Monitoring for BQ Storage Read API session count and estimate egress.

    Returns None on any failure (missing library, missing permission, no data).
    The caller is responsible for printing the terminal warning.
    """
    if table_count <= 0 or total_logical_bytes <= 0:
        return None

    try:
        from google.api_core.exceptions import (
            GoogleAPICallError,
            NotFound,
            PermissionDenied,
        )
        from google.cloud import monitoring_v3
    except ImportError:
        _log.debug("google-cloud-monitoring not installed — skipping egress estimation")
        return None

    try:
        client = monitoring_v3.MetricServiceClient(credentials=credentials)

        now = datetime.now(timezone.utc)
        seconds_ago = lookback_days * 86400
        interval = monitoring_v3.TimeInterval(
            start_time={"seconds": int(now.timestamp()) - seconds_ago},
            end_time={"seconds": int(now.timestamp())},
        )

        # Count CreateReadSession calls — each represents one table read session.
        # The metric serviceruntime.googleapis.com/api/request_count filtered to
        # bigquerystorage.googleapis.com and CreateReadSession gives session count.
        filter_str = (
            'metric.type = "serviceruntime.googleapis.com/api/request_count" '
            'AND resource.labels.service = "bigquerystorage.googleapis.com" '
            'AND metric.labels.method = "google.cloud.bigquery.storage.v1.BigQueryRead.CreateReadSession"'
        )

        request = monitoring_v3.ListTimeSeriesRequest(
            name=f"projects/{project_id}",
            filter=filter_str,
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )

        total_sessions = 0
        for ts in client.list_time_series(request=request):
            for point in ts.points:
                total_sessions += int(point.value.int64_value)

        if total_sessions == 0:
            _log.debug("No Storage Read API sessions found in the last %d days", lookback_days)
            return None

        total_logical_gib = total_logical_bytes / (1024 ** 3)
        avg_gib_per_session = total_logical_gib / table_count
        estimated_egress_gib = total_sessions * avg_gib_per_session

        return EgressEstimate(
            read_sessions=total_sessions,
            estimated_egress_gib=round(estimated_egress_gib, 2),
            lookback_days=lookback_days,
        )

    except PermissionDenied:
        _log.debug("Permission denied for Cloud Monitoring — roles/monitoring.viewer required")
        return None
    except (NotFound, GoogleAPICallError) as exc:
        _log.debug("Cloud Monitoring query failed: %s", exc)
        return None
    except Exception as exc:
        _log.debug("Unexpected error during egress estimation: %s", exc)
        return None
