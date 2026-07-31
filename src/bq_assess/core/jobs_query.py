"""Shared INFORMATION_SCHEMA.JOBS reader for the cost-input modules (R16/R17).

``PricingDetector`` (5.1) and ``WorkloadAnalyzer`` (5.2) both read project-wide job history
from BigQuery; this is the single place that builds the query and runs it, so the
project/region qualification, the completed-query lookback filter, and the SCRIPT exclusion
are defined once.

Two corrections this consolidates (5.x review):
- Uses ``INFORMATION_SCHEMA.JOBS_BY_PROJECT`` — the **whole-project** view — not the bare
  ``JOBS`` alias, which is ``JOBS_BY_USER`` and returns only the *calling identity's* jobs. A
  service account that did not submit the workload would otherwise see an empty job log and the
  Source would be silently mis-assessed (false on-demand / no workload).
- Always applies ``job_type='QUERY' AND state='DONE'`` and a ``creation_time`` lookback bound,
  matching the legacy ``analyzer.py`` idiom; the caller supplies only the SELECT list.

Degrades to ``[]`` on any error (e.g. missing ``bigquery.jobs.listAll``) — the callers treat
no-signal as no-data and never raise (R16.3 / R17.3).
"""

from __future__ import annotations

import logging

from bq_assess.core import pricing_constants as k

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 30

# The one place the SQL-side missing-billed rule lives (hourly + queries reads must agree):
# a job is missing-billed only if it has no billed value AND actually scanned bytes. No-scan
# jobs (MV auto-refreshes, DDL, errored queries) carry NULL for both columns and bill
# nothing — counting them degraded whole windows off the billed basis (2026-07-14 finding).
MISSING_BILLED_COUNTIF = (
    "COUNTIF(total_bytes_billed IS NULL "
    "AND IFNULL(total_bytes_processed, 0) > 0) AS missing_billed_jobs"
)


def read_jobs(
    client,
    project_id: str,
    select_clause: str,
    *,
    days: int = DEFAULT_LOOKBACK_DAYS,
    location: str = "US",
    group_by: str | None = None,
) -> list:
    """Run ``<select_clause> FROM <project>.<region>.JOBS_BY_PROJECT WHERE <completed-query>``.

    ``select_clause`` is the full ``SELECT col, ...`` the caller needs (e.g.
    ``"SELECT reservation_id, edition, statement_type"``). ``group_by`` appends
    ``GROUP BY <expr>`` after the shared WHERE. Returns the row list, or ``[]`` if
    the query cannot be run — never raises.
    """
    sql = (
        f"{select_clause} "
        f"FROM `{project_id}`.`region-{location.lower()}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT "
        f"WHERE job_type = 'QUERY' AND state = 'DONE' "
        f"AND creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY) "
        f"AND {k.V5_JOBS_STATEMENT_TYPE_COLUMN} != '{k.V5_JOBS_SCRIPT_STATEMENT_TYPE}'"
    )
    if group_by:
        sql += f" GROUP BY {group_by}"
    try:
        # location= pins the query job to the data's region — region-qualified
        # INFORMATION_SCHEMA views only resolve when the job runs in that region.
        # Without it, a non-US-default project silently returns no rows (same
        # bug class as the 2026-07-23 TABLE_STORAGE fix in storage_stats.py).
        return list(_retry_query(lambda: client.query(sql, location=location).result()))
    except Exception as exc:  # missing perms / any error → no signal, not a failure
        logger.warning("Could not read INFORMATION_SCHEMA.JOBS_BY_PROJECT: %s", exc)
        return []


def read_jobs_hourly(
    client,
    project_id: str,
    *,
    days: int = DEFAULT_LOOKBACK_DAYS,
    location: str = "US",
) -> list:
    """Read the workload as HOURLY aggregates — ≤ 24×days rows regardless of query volume.

    BigQuery collapses per-job rows server-side into the same UTC-hour buckets
    ``WorkloadAnalyzer._compute`` previously built in Python, so a Source running millions
    of queries/month costs the client ≤720 rows instead of a multi-GB row stream (the
    10 PB-scale OOM found in the 2026-07-08 deep audit). ``missing_billed_jobs`` preserves
    the all-or-nothing ``has_billed_bytes`` semantics: any NULL ``total_bytes_billed`` on a
    job that actually scanned bytes degrades the whole window to the processed-bytes
    fallback. No-scan jobs (NULL/0 ``total_bytes_processed`` — materialized-view
    auto-refreshes, DDL, errored queries) are exempt: they bill nothing, so their NULL
    billed column says nothing about the window's billed basis (2026-07-14 production
    finding: a handful of MV refreshes among 238K billed-carrying jobs degraded the whole
    month to the processed-bytes overestimate). Returns ``[]`` on any error — never
    raises (R17.3).
    """
    select = (
        "SELECT TIMESTAMP_TRUNC(creation_time, HOUR) AS hour_bucket, "
        "SUM(total_slot_ms) AS total_slot_ms, "
        "COUNT(*) AS job_count, "
        "SUM(total_bytes_processed) AS total_bytes_processed, "
        "SUM(total_bytes_billed) AS total_bytes_billed, "
        f"{MISSING_BILLED_COUNTIF}"
    )
    return read_jobs(
        client, project_id, select,
        days=days, location=location, group_by="hour_bucket",
    )


# Cap on distinct query SHAPES exported to queries.jsonl. Ordered by total slot-ms so the
# heaviest statements always survive the cut; the collector logs when truncation occurs.
# Bounds the export the same way read_jobs_hourly bounds the workload read (no per-job
# row stream — the 2026-07-08 deep-audit OOM class).
QUERIES_EXPORT_LIMIT = 50_000

# Server-side query-shape normalization for the GROUP BY key (2026-07-28 scale
# review finding #6). Grouping on EXACT text let a literal-heavy workload fill
# all 50k slots with variants of a few hundred templates — coverage of distinct
# query shapes collapsed while the count looked healthy. These REGEXP_REPLACEs
# mirror analyzer.anonymize_query's literal-stripping (the rule applied
# client-side before export), so the group key IS (approximately) the shape:
#   1. '…' string literals → '?'   (analyzer._STRING_LITERAL_RE: '[^']*')
#   2. numeric literals after an operator/comma/paren/space → ?
# BigQuery's re2 has no lookaround, so the Python rule's lookbehind/lookahead
# become capture groups (\1 … \3). Because each match CONSUMES its trailing
# delimiter, one pass skips every second literal in a run (IN (1,2,3) →
# (?,2,?)) — the numeric replace therefore runs TWICE; the second pass catches
# the odd positions, so comma-separated literal lists fully normalize
# (2026-07-28 MR E review). Any residual divergence from the Python rule is
# strictly FINER grouping, which never merges distinct shapes.
#
# INVARIANT (relied on by collector._queries_from_api_multi's cross-region
# dedup): any two texts in one shape group must Python-anonymize to the SAME
# exported string — Python's rules replace a superset of the spans replaced
# here. Pinned by tests/unit/test_jobs_query.py::TestQueryShapeKey::
# test_same_shape_group_anonymizes_identically.
_QUERY_SHAPE_NUM_PATTERN = "r\"([\\s=><!(,+*/-])(\\d+\\.?\\d*)([\\s,);]|$)\""
QUERY_SHAPE_SQL = (
    "REGEXP_REPLACE(REGEXP_REPLACE("
    "REGEXP_REPLACE(query, r\"'[^']*'\", \"'?'\"), "
    f"{_QUERY_SHAPE_NUM_PATTERN}, r\"\\1?\\3\"), "
    f"{_QUERY_SHAPE_NUM_PATTERN}, r\"\\1?\\3\")"
)


def read_jobs_queries(
    client,
    project_id: str,
    *,
    days: int = DEFAULT_LOOKBACK_DAYS,
    location: str = "US",
    limit: int = QUERIES_EXPORT_LIMIT,
) -> list:
    """Read DISTINCT query SHAPES + aggregate per-statement stats (bounded).

    Groups server-side by the literal-normalized query shape (``QUERY_SHAPE_SQL``)
    so the row count is bounded by workload *shape diversity*, not literal
    variance — 1M runs of one template with different dates form ONE group whose
    slot-ms sums, instead of 1M groups crowding real shapes out of the cap.
    ``ANY_VALUE(query)`` carries one representative text per shape for the
    client-side anonymizer (its literal-stripping then produces the same shape).
    Caps at ``limit`` ordered by total slot-ms (heaviest shapes first).
    NULL-billed jobs are tracked per group via ``missing_billed_jobs`` — same
    billed semantics as the hourly read. Returns ``[]`` on any error — never
    raises (R17.3).
    """
    select = (
        f"SELECT {QUERY_SHAPE_SQL} AS query_shape, "
        "ANY_VALUE(query) AS query, "
        "SUM(total_slot_ms) AS total_slot_ms, "
        "COUNT(*) AS job_count, "
        "SUM(total_bytes_processed) AS total_bytes_processed, "
        "SUM(total_bytes_billed) AS total_bytes_billed, "
        f"{MISSING_BILLED_COUNTIF}, "
        "ANY_VALUE(statement_type) AS statement_type, "
        "MAX(creation_time) AS creation_time"
    )
    return read_jobs(
        client, project_id, select,
        days=days, location=location,
        group_by=f"query_shape ORDER BY total_slot_ms DESC LIMIT {int(limit)}",
    )


def read_reservation_groups(
    client,
    project_id: str,
    *,
    days: int = DEFAULT_LOOKBACK_DAYS,
    location: str = "US",
) -> list:
    """Read the pricing-model signal as ``GROUP BY reservation_id, edition`` counts.

    A project has a handful of distinct (reservation_id, edition) pairs, so this returns
    a handful of rows where the per-job read returned millions. NULL-ness of
    ``reservation_id`` is preserved per group — the classification signal (V5) is intact.
    Returns ``[]`` on any error — never raises (R16.3).
    """
    select = (
        f"SELECT {k.V5_JOBS_RESERVATION_ID_COLUMN}, {k.V5_JOBS_EDITION_COLUMN}, "
        f"COUNT(*) AS job_count"
    )
    return read_jobs(
        client, project_id, select,
        days=days, location=location,
        group_by=f"{k.V5_JOBS_RESERVATION_ID_COLUMN}, {k.V5_JOBS_EDITION_COLUMN}",
    )


def _retry_query(fn):
    """Execute fn with retries — delegates to the scanner's shared ladder.

    Previously a weaker parallel implementation (3 retries, no Retry-After, no
    jitter, no transport-error handling) that gave up in ~7s under the same
    sustained-throttling event the scanner survives, silently degrading the
    workload/pricing signal to [] (2026-07-28 review). One retry helper now
    governs every BigQuery call in the collection path.
    """
    from bq_assess.core.scanner import _retry

    return _retry(fn)
