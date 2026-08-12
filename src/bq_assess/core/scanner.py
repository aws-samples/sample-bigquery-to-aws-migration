"""BigQuery metadata scanner — yields normative EntityMetadata (R2, R3, R4, R23.1-.2).

Read-only: captures table/view/mview/routine *metadata and definitions* only; never
executes SELECT against data rows (R22.2). Resilient: transient API errors (429/500/503)
retried with exponential backoff (R23.1); per-entity failures recorded and skipped (R23.2).

Implements the design.md § Component Interfaces ``BigQueryScanner`` contract — the frozen
seam other modules build on is ``scan() -> Iterator[EntityMetadata]`` and
``self.failures: list[FailureRecord]``. (Issue #6 / 1.1.)
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests.exceptions
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from google.oauth2 import service_account

from bq_assess.core.classifier import classify_population
from bq_assess.models import (
    ColumnSchema,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    FailureRecord,
    RangePartitionConfig,
    RoutineMetadata,
    TimePartitionConfig,
)

logger = logging.getLogger(__name__)

# Retry configuration for transient BigQuery API errors (R23.1, amended
# 2026-07-28 — see requirements.md R23.1 note). 429s get a deeper ladder than
# 500/503: throttling is guaranteed-recoverable (the quota refills), and giving
# up after 7s converts backpressure into silently dropped entities at
# 100k-table scale. Budgets are tracked PER ERROR CLASS — a 503 arriving after
# a burst of 429s gets its own full budget, not the 429s' leftovers.
RETRY_CONFIG = {
    "max_retries": 3,
    "max_retries_throttled": 8,
    "initial_delay_seconds": 1.0,
    "backoff_multiplier": 2.0,  # 1s, 2s, 4s, ...
    "max_delay_seconds": 60.0,
    "retryable_status_codes": [429, 500, 503],
}

# Global request budget: just under BigQuery's ~100 req/s/user default quota for
# tables.get, shared by ALL scanner threads. Keeps 50-way concurrency from
# sitting in sustained-429 territory on large projects.
DEFAULT_MAX_REQUESTS_PER_SECOND = 80.0


class RateLimiter:
    """Global request-rate limiter shared across scanner threads.

    ``acquire()`` blocks until the caller's turn. Implementation is a computed
    sleep-slot rather than a token-bucket spin loop: under the lock each caller
    atomically claims the next 1/rate-spaced timestamp, then sleeps exactly once
    until its slot — one lock acquisition and at most one sleep per acquire, no
    thundering-herd wakeups when many threads are blocked (a spin-loop variant
    woke every waiter per token; 2026-07-28 review). Allows a 1-second burst
    (slots may be claimed up to 1s in the past).
    """

    def __init__(self, max_per_second: float = DEFAULT_MAX_REQUESTS_PER_SECOND) -> None:
        self._interval = 1.0 / float(max_per_second)
        self._next_slot = time.monotonic() - 1.0  # allow an initial burst
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(self._next_slot, now - 1.0)  # burst window: 1s of backlog
            self._next_slot = slot + self._interval
        wait = slot - now
        if wait > 0:
            time.sleep(wait)

# BigQuery table_type string -> EntityType (R4.1). Unknown types fall back to TABLE.
_TABLE_TYPE_MAP: dict[str, EntityType] = {
    "TABLE": EntityType.TABLE,
    "EXTERNAL": EntityType.EXTERNAL,
    "VIEW": EntityType.VIEW,
    "MATERIALIZED_VIEW": EntityType.MATERIALIZED_VIEW,
}


class ScannerError(Exception):
    """Raised when the scanner encounters a fatal error (auth, project-not-found)."""


class ScanAbortedError(ScannerError):
    """Raised mid-scan when the failure rate crosses the abort threshold.

    Lives in the scanner (which owns ``self.failures`` and sees every attempt,
    including failure-only tails and dataset-listing failures) rather than in a
    consumer's yield loop, where a failure-only tail never re-enters the loop
    body (2026-07-28 review). Carries the counts for the caller's message.
    """

    def __init__(self, message: str, *, entity_failures: int, listing_failures: int,
                 attempted: int) -> None:
        super().__init__(message)
        self.entity_failures = entity_failures
        self.listing_failures = listing_failures
        self.attempted = attempted


# Abort thresholds: don't judge the rate until a meaningful sample has been
# attempted — small projects can have a couple of flaky entities. Listing
# failures are tracked SEPARATELY from entity failures because one failed
# list_tables hides an unknown number of entities (counting it as one record
# let a 40-dataset permission revocation slip a 0.08% rate past a 5% wire).
ABORT_MIN_ATTEMPTED = 500
ABORT_ENTITY_FAILURE_RATE = 0.05
ABORT_MAX_LISTING_FAILURES = 3

# Datasets scanned per pipeline chunk: listings for a chunk run in parallel,
# then its entity fetches stream out before the next chunk starts. Bounds
# resident Futures/Table objects to one chunk (a single project-wide dict
# held every result for the whole scan — multi-GB at 100k tables) while still
# batching listings (serial per-dataset listing cost thousands of round trips).
DATASET_CHUNK_SIZE = 25


def population_for(entity_type: EntityType) -> EntityPopulation:
    """Deprecated shim — delegates to the canonical classifier (issue #7).

    Kept as a thin alias so any external caller of the old name still works; new code
    should import ``classify_population`` from ``core/classifier`` directly. (The #6 seam
    note is resolved: the classifier is now the single source of truth.)
    """
    return classify_population(entity_type)


class BigQueryScanner:
    """Scans BigQuery project metadata with retry logic and per-entity resilience.

    Supports service-account JSON credentials or Application Default Credentials.
    Accesses metadata and object definitions only — never reads data rows (R22.2).
    """

    def __init__(
        self,
        project_id: str,
        credentials_path: str | None = None,
        use_adc: bool = False,
        max_concurrent_requests: int = 16,
        max_requests_per_second: float = DEFAULT_MAX_REQUESTS_PER_SECOND,
    ) -> None:
        self._project_id = project_id
        self._credentials_path = credentials_path
        self._use_adc = use_adc
        self._max_concurrent = max_concurrent_requests
        self._rate_limiter = RateLimiter(max_requests_per_second)
        self._client: bigquery.Client | None = None
        self.failures: list[FailureRecord] = []
        self.dataset_locations: dict[str, str] = {}  # dataset_id → BQ region (from scanned Tables)
        self._listing_failures = 0
        self._scanned_count = 0

    # ------------------------------------------------------------------
    # Client initialisation
    # ------------------------------------------------------------------

    def _get_client(self) -> bigquery.Client:
        """Lazily create and return the BigQuery client (read-only scope)."""
        if self._client is not None:
            return self._client

        if self._credentials_path:
            credentials = service_account.Credentials.from_service_account_file(
                self._credentials_path,
                scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
            )
            self._client = bigquery.Client(
                project=self._project_id, credentials=credentials
            )
        elif self._use_adc:
            self._client = bigquery.Client(project=self._project_id)
        else:
            raise ScannerError(
                "No credentials provided. Supply a service account JSON path "
                "or enable Application Default Credentials (--use-adc)."
            )

        self._expand_connection_pool(self._client)
        return self._client

    def _expand_connection_pool(self, client: bigquery.Client) -> None:
        """Resize the HTTP connection pool to match concurrency, preventing pool-full warnings."""
        from requests.adapters import HTTPAdapter

        pool_size = self._max_concurrent + 10
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        client._http.mount("https://", adapter)
        client._http.mount("http://", adapter)

    # ------------------------------------------------------------------
    # Credential validation (R2)
    # ------------------------------------------------------------------

    def validate_credentials(self) -> bool:
        """Run a lightweight metadata-only call to verify read access (R2.1).

        Returns ``True`` on success; raises :class:`ScannerError` with a descriptive
        message distinguishing invalid-credentials / insufficient-permissions /
        project-not-found on failure (R2.2).
        """
        try:
            client = self._get_client()
            _ = list(client.list_datasets(max_results=1))
            return True
        except ScannerError:
            raise
        except Exception as exc:
            raise ScannerError(_describe_auth_error(exc)) from exc

    # ------------------------------------------------------------------
    # Scanning (R3, R4, R23)
    # ------------------------------------------------------------------

    def scan(
        self,
        dataset_filter: list[str] | None = None,
        skip_datasets: set[str] | None = None,
        on_chunk_complete=None,
    ) -> Iterator[EntityMetadata]:
        """Yield :class:`EntityMetadata` for every entity in the project.

        Scans, per dataset: tables/views/materialized-views/external (via ``list_tables``
        + ``get_table``) and persistent routines (via ``list_routines``). When
        ``dataset_filter`` is provided, only those datasets are scanned; otherwise all
        datasets in the project (R3.4).

        Checkpoint/resume support (R5, 2026-07-28): ``skip_datasets`` excludes
        datasets a prior interrupted scan already completed; ``on_chunk_complete``
        is called after each dataset chunk fully streams out, with
        ``(chunk_dataset_ids, chunk_entities)`` — the consistency point at which
        a caller can durably checkpoint. Chunks contain whole datasets, so a
        checkpointed dataset is never half-scanned.

        Transient API errors are retried (R23.1). Per-entity errors are recorded in
        ``self.failures`` and skipped so one bad entity never aborts the scan (R23.2).
        """
        client = self._get_client()
        self.failures = []
        self.dataset_locations = {}
        self._listing_failures = 0
        self._scanned_count = 0

        datasets = self._list_datasets_with_retry(client)
        if dataset_filter:
            filter_set = set(dataset_filter)
            datasets = [ds for ds in datasets if ds.dataset_id in filter_set]
        if skip_datasets:
            datasets = [ds for ds in datasets if ds.dataset_id not in skip_datasets]

        # One pool for the whole scan, consumed in DATASET_CHUNK_SIZE chunks:
        # a chunk's listings run in parallel, then its entity fetches stream out
        # before the next chunk starts. This keeps listings batched (serial
        # per-dataset listing was thousands of round trips) while bounding
        # resident Futures/Table objects to one chunk and keeping entities
        # flowing to the consumer from the first chunk (a whole-project futures
        # dict blocked every yield behind ALL listings and held every result
        # until the scan ended — 2026-07-28 review). On early exit
        # (KeyboardInterrupt, GeneratorExit) pending futures are cancelled.
        pool = ThreadPoolExecutor(max_workers=self._max_concurrent)
        try:
            for start in range(0, len(datasets), DATASET_CHUNK_SIZE):
                chunk = datasets[start:start + DATASET_CHUNK_SIZE]
                if on_chunk_complete is None:
                    yield from self._scan_dataset_chunk(client, pool, chunk)
                else:
                    chunk_entities: list[EntityMetadata] = []
                    for entity in self._scan_dataset_chunk(client, pool, chunk):
                        chunk_entities.append(entity)
                        yield entity
                    on_chunk_complete(
                        [d.dataset_id for d in chunk], chunk_entities
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _scan_dataset_chunk(
        self, client: bigquery.Client, pool: ThreadPoolExecutor, datasets: list
    ) -> Iterator[EntityMetadata]:
        """List one chunk of datasets in parallel, then stream its entity fetches."""
        list_futures: dict[Future, tuple[str, str]] = {}
        for dataset_ref in datasets:
            ds = dataset_ref.dataset_id
            list_futures[pool.submit(self._list_tables_with_retry, client, ds)] = ("tables", ds)
            list_futures[pool.submit(self._list_routines_with_retry, client, ds)] = ("routines", ds)

        fetch_futures: dict[Future, tuple[str, str, str]] = {}
        for future in as_completed(list_futures):
            kind, ds = list_futures[future]
            try:
                items = future.result()
            except Exception as exc:  # dataset-level failure → record + continue (R23.2)
                name = ds if kind == "tables" else f"{ds} (routines)"
                logger.error("Failed to list %s in %s: %s", kind, ds, exc)
                self.failures.append(
                    FailureRecord(entity_name=name, stage="scan", error=str(exc))
                )
                if kind == "tables":
                    self._listing_failures += 1
                    self._check_abort()
                continue
            if kind == "tables":
                for item in items:
                    fetch_futures[
                        pool.submit(self._get_table_with_retry, client, item.reference)
                    ] = (f"{item.dataset_id}.{item.table_id}", "table", ds)
            else:
                for r in items:
                    fetch_futures[
                        pool.submit(self._get_routine_with_retry, client, r.reference)
                    ] = (f"{ds}.{r.routine_id}", "routine", ds)

        for future in as_completed(fetch_futures):
            full_name, kind, ds = fetch_futures.pop(future)  # free result refs as consumed
            try:
                result = future.result()
                if kind == "table":
                    # Capture the dataset's region from the Table we already
                    # fetched — the collector's multi-region routing needs a
                    # dataset→location map, and deriving it here costs zero
                    # extra API calls (a per-dataset get_dataset loop was a
                    # serial, unretried scale regression — 2026-07-28 review).
                    loc = getattr(result, "location", None)
                    if loc:
                        self.dataset_locations.setdefault(ds, loc)
                    entity = _to_entity_metadata(result)
                else:
                    entity = _routine_to_entity(result, ds)
            except Exception as exc:  # per-entity failure → record + skip (R23.2)
                logger.error("Failed to scan entity %s: %s", full_name, exc)
                self.failures.append(
                    FailureRecord(entity_name=full_name, stage="scan", error=str(exc))
                )
                self._check_abort()
                continue
            self._scanned_count += 1
            yield entity

    def _check_abort(self) -> None:
        """Raise ScanAbortedError when failures cross the abort thresholds.

        Runs on every recorded failure — including failure-only tails that never
        reach a consumer's yield loop. Entity failures are judged as a rate;
        dataset-listing failures are judged by count, because each one hides an
        unknown number of entities and would otherwise be under-weighted.
        """
        if self._listing_failures > ABORT_MAX_LISTING_FAILURES:
            raise ScanAbortedError(
                f"{self._listing_failures} dataset listings failed — each hides an "
                "unknown number of entities; the bundle would silently understate "
                "the estate.",
                entity_failures=len(self.failures) - self._listing_failures,
                listing_failures=self._listing_failures,
                attempted=self._scanned_count + len(self.failures),
            )
        entity_failures = len(self.failures) - self._listing_failures
        attempted = self._scanned_count + entity_failures
        if (
            attempted >= ABORT_MIN_ATTEMPTED
            and entity_failures / attempted > ABORT_ENTITY_FAILURE_RATE
        ):
            raise ScanAbortedError(
                f"{entity_failures} of {attempted} entities failed to scan "
                f"(>{ABORT_ENTITY_FAILURE_RATE:.0%}).",
                entity_failures=entity_failures,
                listing_failures=self._listing_failures,
                attempted=attempted,
            )

    # ------------------------------------------------------------------
    # Retry wrappers
    # ------------------------------------------------------------------

    # retry=None on every client call: the library's built-in DEFAULT_RETRY
    # (600s deadline, retries the same 429/transport errors) would otherwise run
    # INSIDE our _retry — two stacked ladders multiplying worst-case waits
    # (2026-07-28 review). Exactly one ladder (ours) governs.
    #
    # The rate limiter is threaded into _retry so every attempt — first try and
    # retries alike — consumes a token; for paginated listings each page fetch
    # is charged so token accounting matches wire traffic.

    def _list_datasets_with_retry(self, client: bigquery.Client) -> list:
        return _retry(
            lambda: self._drain_pages(client.list_datasets(retry=None)),
            rate_limiter=self._rate_limiter,
        )

    def _list_tables_with_retry(self, client: bigquery.Client, dataset_id: str) -> list:
        return _retry(
            lambda: self._drain_pages(client.list_tables(dataset_id, retry=None)),
            rate_limiter=self._rate_limiter,
        )

    def _list_routines_with_retry(
        self, client: bigquery.Client, dataset_id: str
    ) -> list:
        return _retry(
            lambda: self._drain_pages(client.list_routines(dataset_id, retry=None)),
            rate_limiter=self._rate_limiter,
        )

    def _drain_pages(self, iterator) -> list:
        """Materialize a paged iterator, charging one token per page fetch.

        The _retry wrapper already charged page 1's token; subsequent pages are
        separate HTTP requests and must not ride free (a 10k-table dataset is
        dozens of page fetches under what was a single token).
        """
        pages = getattr(iterator, "pages", None)
        if pages is None:  # plain iterable (test doubles) — nothing paginated
            return list(iterator)
        items: list = []
        for i, page in enumerate(pages):
            if i > 0:
                self._rate_limiter.acquire()
            items.extend(page)
        return items

    def _get_table_with_retry(
        self, client: bigquery.Client, table_ref: bigquery.TableReference
    ) -> bigquery.Table:
        return _retry(
            lambda: client.get_table(table_ref, retry=None),
            rate_limiter=self._rate_limiter,
        )

    def _get_routine_with_retry(self, client: bigquery.Client, routine_ref):
        return _retry(
            lambda: client.get_routine(routine_ref, retry=None),
            rate_limiter=self._rate_limiter,
        )


# ======================================================================
# Module-level helpers
# ======================================================================


def _retry_after_seconds(exc: GoogleAPICallError) -> float | None:
    """Extract a Retry-After hint (seconds) from the API response, if present."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# Raw transport errors (connection reset, socket timeout) — transient like a 503,
# but they don't subclass GoogleAPICallError so the old retry let them fail the
# entity on the first attempt (2026-07-27 scale review).
_TRANSPORT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _retry(fn, config: dict | None = None, rate_limiter: RateLimiter | None = None):
    """Execute *fn* with exponential-backoff retries on transient errors (R23.1).

    - Budgets are tracked per error class (429 vs 500/503 vs transport), so a
      mixed-code storm can't starve one class of its retries.
    - 429s use the deeper ``max_retries_throttled`` ladder and honor the
      server's Retry-After header — clamped to ``max_delay_seconds`` and
      jittered so synchronized workers don't re-slam the API in one wave.
    - When *rate_limiter* is given, every attempt (including retries) consumes
      a token — retry traffic must not bypass the global request budget.
    """
    cfg = config or RETRY_CONFIG
    max_retries: int = cfg["max_retries"]
    max_retries_throttled: int = cfg.get("max_retries_throttled", max_retries)
    delay: float = cfg["initial_delay_seconds"]
    multiplier: float = cfg["backoff_multiplier"]
    max_delay: float = cfg.get("max_delay_seconds", 60.0)
    retryable: set[int] = set(cfg["retryable_status_codes"])

    used: dict[str, int] = {"throttled": 0, "server": 0, "transport": 0}
    while True:
        try:
            if rate_limiter is not None:
                rate_limiter.acquire()
            return fn()
        except (GoogleAPICallError, *_TRANSPORT_ERRORS) as exc:
            is_api = isinstance(exc, GoogleAPICallError)
            if is_api and exc.code not in retryable:
                raise
            if is_api and exc.code == 429:
                klass, budget = "throttled", max_retries_throttled
            elif is_api:
                klass, budget = "server", max_retries
            else:
                klass, budget = "transport", max_retries
            if used[klass] >= budget:
                raise
            used[klass] += 1
            hinted = _retry_after_seconds(exc) if is_api else None
            if hinted is not None:
                # Hint is a floor: clamp to max_delay, then add upward spread so
                # simultaneously-throttled workers don't wake in one wave.
                wait = min(hinted, max_delay) * (1.0 + 0.5 * random.random())
            else:
                wait = min(delay, max_delay) * (0.5 + random.random())
            logger.warning(
                "Transient error (%s), retrying in %.1fs (%s attempt %d/%d)",
                exc.code if is_api else type(exc).__name__,
                wait,
                klass,
                used[klass],
                budget,
            )
            time.sleep(wait)
            delay = min(delay * multiplier, max_delay)


def _describe_auth_error(exc: Exception) -> str:
    """Return a user-friendly description for auth/permission errors (R2.2)."""
    msg = str(exc).lower()
    if "403" in msg or "permission" in msg:
        return (
            f"Insufficient permissions for project: {exc}. "
            "Ensure the principal has the bigquery.metadataViewer (or dataViewer) role."
        )
    if "404" in msg or "not found" in msg:
        return f"Project not found or inaccessible: {exc}"
    if "invalid" in msg or "credential" in msg or "401" in msg:
        return f"Invalid credentials: {exc}"
    return f"Credential validation failed: {exc}"


def _entity_type_for(table: bigquery.Table) -> EntityType:
    """Map a BigQuery table_type to EntityType (R4.1); unknown → TABLE."""
    return _TABLE_TYPE_MAP.get((table.table_type or "TABLE").upper(), EntityType.TABLE)


def _to_entity_metadata(table: bigquery.Table) -> EntityMetadata:
    """Convert a ``bigquery.Table`` to :class:`EntityMetadata` (tables/views/mviews)."""
    entity_type = _entity_type_for(table)
    population = classify_population(entity_type)

    columns = [_to_column_schema(f) for f in table.schema] if table.schema else []

    time_part = _to_time_partition(table)
    range_part = _to_range_partition(table)  # R3.8 — captured distinctly
    clustering = list(table.clustering_fields) if table.clustering_fields else None

    view_query = getattr(table, "view_query", None)
    mview_query = getattr(table, "mview_query", None)

    depends_on = _extract_dependencies(view_query or mview_query)

    return EntityMetadata(
        entity_id=table.table_id,
        dataset_id=table.dataset_id,
        full_name=f"{table.dataset_id}.{table.table_id}",
        entity_type=entity_type,
        population=population,
        num_rows=table.num_rows or 0,
        num_bytes=table.num_bytes or 0,
        columns=columns,
        time_partitioning=time_part,
        range_partitioning=range_part,
        clustering_fields=clustering,
        view_query=view_query,
        mview_query=mview_query,
        routine=None,
        depends_on=depends_on,
        last_modified=_normalize_modified(table.modified),
    )


def _routine_to_entity(routine, dataset_id: str) -> EntityMetadata:
    """Convert a fully-fetched routine to a ROUTINE :class:`EntityMetadata` (R3.3)."""

    arguments = [
        arg.name or f"arg{i}"
        for i, arg in enumerate(routine.arguments or [])
    ]
    routine_meta = RoutineMetadata(
        name=routine.routine_id,
        language=routine.language or "SQL",
        arguments=arguments,
        body=routine.body or "",
        routine_type=str(routine.type_ or "SCALAR_FUNCTION"),
    )

    depends_on = _extract_dependencies(routine.body)

    return EntityMetadata(
        entity_id=routine.routine_id,
        dataset_id=dataset_id,
        full_name=f"{dataset_id}.{routine.routine_id}",
        entity_type=EntityType.ROUTINE,
        population=EntityPopulation.REBUILT,
        num_rows=0,
        num_bytes=0,
        columns=[],
        time_partitioning=None,
        range_partitioning=None,
        clustering_fields=None,
        view_query=None,
        mview_query=None,
        routine=routine_meta,
        depends_on=depends_on,
        last_modified=_normalize_modified(getattr(routine, "modified", None)),
    )


def _to_time_partition(table: bigquery.Table) -> TimePartitionConfig | None:
    """Capture time partitioning; field=None models ingestion-time (_PARTITIONTIME)."""
    tp = table.time_partitioning
    if tp is None:
        return None
    return TimePartitionConfig(type=tp.type_ or "DAY", field=tp.field)


def _to_range_partition(table: bigquery.Table) -> RangePartitionConfig | None:
    """Capture range partitioning distinctly from time partitioning (R3.8)."""
    rp = getattr(table, "range_partitioning", None)
    if rp is None:
        return None
    rng = getattr(rp, "range_", None)
    return RangePartitionConfig(
        field=rp.field,
        start=int(getattr(rng, "start", 0) or 0),
        end=int(getattr(rng, "end", 0) or 0),
        interval=int(getattr(rng, "interval", 1) or 1),
    )


def _to_column_schema(field: bigquery.SchemaField) -> ColumnSchema:
    """Recursively convert a BigQuery ``SchemaField`` to :class:`ColumnSchema`.

    Nesting is preserved (no flattening) — STRUCT/RECORD children recurse (R6.2).
    """
    nested = [_to_column_schema(f) for f in field.fields] if field.fields else []
    return ColumnSchema(
        name=field.name,
        field_type=field.field_type,
        mode=field.mode or "NULLABLE",
        fields=nested,
    )


def _extract_dependencies(sql: str | None) -> list[str]:
    """Best-effort parse of referenced tables from view/mview/routine SQL (R4.5).

    Returns ``dataset.table`` FQNs (project prefix stripped), de-duplicated,
    order-stable. Parsing lives in core/table_refs (shared grammar, 2026-08-04);
    the policy here differs from attribution's: NO project filter — a view
    reading another project's table still depends on it, and the dependency
    should surface in depends_on for the relationship pass to consider.
    """
    if not sql:
        return []
    from bq_assess.core.table_refs import extract_table_refs
    seen: dict[str, None] = {}
    for ref in extract_table_refs(sql, project_id=None):
        if ref.dataset is None:
            continue  # bare names (aliases/CTEs) are not dependencies
        seen.setdefault(ref.dataset_table, None)
    return list(seen.keys())


def _normalize_modified(modified: datetime | None) -> datetime:
    """Return a tz-aware UTC datetime for the entity's last-modified time."""
    if modified is None:
        return datetime.now(timezone.utc)
    if modified.tzinfo is None:
        return modified.replace(tzinfo=timezone.utc)
    return modified
